#!/usr/bin/env python3
"""WAVE REGISTRY for the market-wave playbook (Jack 2026-11-07: "make this almost
into a playbook where when invoking the skill, it does its thing and shares all
the parameters / assumptions it made, and asks if i want to tune any").

A wave = an entry in run-logs/pvd-scan/waves.json. The relight task ("KL pvd-scan
waves", pass 7) runs every enabled wave each 15 min: pvd_scan.py (relit / new
programs, any-wave mode) then pvd_wave_tune.py with the wave's tune args. Pass 0
(pvd_watch_keepalive.ps1) keeps its dashboard up, and pass 6 / the 11/04
dashboard leave registry series alone. Adding a wave never edits the task.

    python pvd_waves.py add NAME --series A,B --since ISO [--preset default|2028]
                        [--port N] [--budget USD] [--scan-size N] [--stop-cap ISO] [--note TEXT]
    python pvd_waves.py run [--write] [--only NAME]       # scan + tune (dry run without --write)
    python pvd_waves.py dash NAME|all                     # start dashboards, detached
    python pvd_waves.py report NAME [--detect detect.json] [--out report.md]
    python pvd_waves.py setarg NAME FLAG [VALUE] [--unset]   # FLAG without dashes, e.g. cheap-tiers 2:20,5:10
    python pvd_waves.py set NAME KEY VALUE                # scan_size / budget / stop_cap / enabled / ...
    python pvd_waves.py list | remove NAME
    python pvd_waves.py stop NAME [--reason TEXT] [--dry-run]   # disable + its live entries -> "stopped"
    python pvd_waves.py preflight [NAME] [--series A,B --near C] [--detect detect.json] [--auto]
    python pvd_waves.py pickoffs [NAME|all] [--hours 6] [--act] [--update-state] [--json OUT]
    python pvd_waves.py darkcheck [--min-age 20] [--act] [--update-state] [--json OUT]
    python pvd_waves.py pace NAME [--aim 1.20] [--json OUT]          # under-$1 breakdown (read-only)
    python pvd_waves.py resize NAME [--aim 1.20 --max-mult 5] [--apply]   # size to the $1 floor (Jack's word)
    python pvd_waves.py whydark [NAME|all] [--json OUT]              # why each side isn't resting (read-only)

PASS LOCK (2026-11-14): `run --write` holds run-logs/pvd-scan/wave_pass_NAME.lock from
reading a wave's args through its tune; setarg / set / stop wait for it, the task's pass
skips a wave another pass holds, `run --only NAME` waits (--wait-lock SECS). A setarg can
no longer be written back over by a pass that started before it (FX 04:02Z 11/14).
SCAN BUDGET (2026-11-14): a wave's scan may always use its own `budget` against its own
live listings, besides the shared PVD_SCAN_BUDGET + budget across every wave.

DARK CHECK (Jack 2026-11-11: "ELI5 why market-wave didnt autoquote last night, and
what needs to be fixed so that never happens again" -> "yes build all 3. make no
mistakes, this is serious"): on 11/11 the 2028 relight was LISTED (724 markets; the
overnight texts said "247 / 209 / 199 markets greenlit") and rested NOTHING -- the
11/08 UNWIND_ONLY_WAVES lock covered those old-wave entries, and nothing compared
the list with what rests on Kalshi. darkcheck does, every 15 min (relight task pass
9): each listed wave market 20+ min into its listing is two-sided, one-sided or
DARK on Kalshi, and a dark one gets its reason (the old-wave lock, a pause, a held
market's unwind, or the bot's own selection decision). Exit 3 = new dark markets to
alert on (wave_alert.py dark). --act fixes the lock case itself: a series of an
enabled registry wave is re-run now (its tune stamps wave_added), any other is
adopted into a registry wave on the default preset after the auto preflight.

The DEFAULT preset is deliberately conservative for an unknown wave (breakers on,
modest cheap-side tiers, 90c+ sides capped at 1); the report lists every value
and the tune menu. "2028" = the 11/07 end state of the 2028 election wave --
Jack's word only (it carries --no-halts and tiers up to x45 of the base).

PICK-OFF GATES (Jack 2026-11-08: "the pickoffs yesterday were unacceptable"; the
11/07 House night lost ~$2.9k to baited books, 53 adding fills ~$4.3k that day):
  preflight  PASS / WARN / FAIL for every guard a wave relies on -- the switch
             files (HALT, UNWIND_ONLY*), the bot halted or not cycling, the bot's
             own guards as its latest startup printed them (join wait on penny +
             0.1c books with rest memory, off-usual size, sweep breaker, tape
             trigger, bait stop, company KPIs off), the wave's tune args (no
             --no-halts / --size-mult / tiers above the default preset without
             Jack, the re-anchor cap on, 90c+ side cap, twin block) and its scope
             (5+ events a series, FIT / NEAR only, no company KPIs or blocked
             series). Exit 1 on any FAIL. `add --auto` runs it and lists nothing
             on a FAIL.
  pickoffs   every fill that OPENED or ADDED to a position on a wave market in
             the window, marked to Kalshi's book with OUR orders taken out (our
             own quotes prop up thin mids), flagged at 5c+ and $25+ against us;
             3+ flagged markets in a series = a series trip. --act pins a flagged
             registry-wave market (or a tripped series) at the wave's base size
             (Jack 11/07: "scale it down to base contract sizes") and re-tunes it
             now; legacy waves are reported for a hand edit. Exit 3 = new ones.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
REGISTRY = os.environ.get("PVD_WAVES_REGISTRY") or os.path.join(HERE, "run-logs", "pvd-scan", "waves.json")
LIST = os.path.join(HERE, "run-logs", "incentive-mm", "post_vote_dead_books.json")
BOT_LOG_DIR = os.path.join(HERE, "run-logs", "incentive-mm")
GLOBAL_BUDGET = float(os.environ.get("PVD_SCAN_BUDGET", "300000"))
USED_PORTS = {8765, 8766, 8771, 8772, 8773, 8774}      # ENS page, political, combo_dash, 2028, 11/04
INC = os.path.join(HERE, "run-logs", "incentive-mm")
STATE_FILE = os.path.join(INC, "imm_state.json")
LAUNCHER = os.path.join(HERE, "run_incentive_mm.ps1")
TASK_CMD = os.path.join(HERE, "run_pvd_scan_waves.cmd")
PICK_STATE = os.environ.get("PVD_PICKOFF_STATE") or os.path.join(HERE, "run-logs", "pvd-scan", "pickoff_state.json")
SCAN_LOCK = os.path.join(HERE, "run-logs", "pvd-scan", "scan.lock")     # shared with pvd_scan / the tasks

PRESETS = {
    # an unknown wave: quote everything fast, keep the bot's breakers, size modestly.
    # The re-anchor cap is spelled out (pvd_wave_tune's defaults): toward the taker a
    # limit moves at most 5c a run and holds a run after a 10c+ touch jump -- the
    # 11/07 House night's baits were followed onto the jumped touch (Jack 2026-11-08:
    # "Cap the reanchor").
    "default": [
        "--room", "3", "--price-min", "1", "--price-max", "99",
        "--flip-through", "--follow-book", "--cushion", "0", "--reanchor", "3",
        "--reanchor-step", "5", "--reanchor-jump", "10",
        "--bank-size", "200", "--bank-max", "300", "--bank-by-hrs", "6", "--bank-usd", "1.30",
        "--no-drop", "--hold-max",
        # cheap sides only (11/08 review): 0 fills at 1-2c in ~28 h, while every pick-off
        # sat at 12-80c, where the old 15c x3 / 30c x2 / 70c x1.5 tiers multiplied the
        # size (KXCCLA's 600 @ 8c was the 15c x3 tier). Mid-price tiers are Jack's call,
        # after a day with no pick-offs.
        "--cheap-tiers", "2:10,5:5",
        # no 90c+ side cap (Jack 2026-11-08: "dont preset that"): sides tying up
        # 90c+ quote like any other; --side-cap stays a tune lever
        "--twin-block", "200",
    ],
    # NEAR series (settle in 1-14 days) -- the 11/04 midterm treatment: the scanner's
    # dead/live kinds at 120 / 80 a side (300 on a decided side), the bot's kind cushions
    # (dead 2c / live 5c off the last trade) and default 3-97c band, no tiers, multipliers
    # or bank-early; flip-through for exits, untraded limits re-anchored (capped), no 90c+ side cap (11/08)
    "near": [
        "--flip-through", "--reanchor", "3", "--reanchor-step", "5", "--reanchor-jump", "10",
        "--twin-block", "200",
    ],
    # the 2028 election wave as of 11/07 18:00Z (long-dated, low adverse selection) --
    # JACK'S WORD ONLY: --no-halts + --size-mult + tiers up to x45 are what the 11/07
    # House-night baits fed on; `add --auto` refuses it and preflight FAILs it in auto
    "2028": [
        "--room", "3", "--price-min", "1", "--price-max", "99",
        "--flip-through", "--no-halts", "--follow-book", "--cushion", "0", "--reanchor", "3",
        "--bank-size", "200", "--bank-max", "300", "--bank-by-hrs", "6", "--bank-usd", "1.30",
        "--no-drop", "--hold-max", "--size-mult", "1.5",
        "--cheap-tiers", "2:45,5:15,15:11.25,30:6.75,50:5.0625,70:2.25",
        "--twin-block", "200",
    ],
}
ARG_NOTES = {
    "--room": "c of extra room each way on the scan-time limits (once per entry)",
    "--price-min": "lowest price a rung may rest at (c)",
    "--price-max": "highest price a rung may rest at (c)",
    "--flip-through": "after a fill, the side that reduces the position rests size + position",
    "--no-halts": "series exempt from the sweep breaker / toxic halts; wave breaker (2 fills of 200+ at 90c+ in 10 min -> 15 min hold) + 200+ tape trigger instead",
    "--follow-book": "limits follow the book, not a stale last trade",
    "--cushion": "c the guard keeps off the last trade (0 = none)",
    "--reanchor": "limits = touch +- this c, every run",
    "--reanchor-step": "toward the taker a limit moves at most this c a run (0 = uncapped: never)",
    "--reanchor-jump": "a side whose touch jumped this c+ toward the taker since the last run holds a run (bait)",
    "--halts-on": "clear no_halts from the wave's entries (restores the sweep breaker / toxic halts)",
    "--bank-size": "a side rests this until the market banks --bank-usd this period",
    "--bank-max": "...or this where --bank-size won't bank by --bank-by-hrs before the stop",
    "--bank-by-hrs": "hours before the period's end to have banked by",
    "--bank-usd": "$ a market should bank per period (Kalshi pays only from $1; ~1.15x the estimate)",
    "--no-drop": "a market that has banked keeps its size",
    "--hold-max": "a market raised to --bank-max stays there for its period",
    "--size-mult": "every size x this (tiers multiply it)",
    "--cheap-tiers": "C:X -- a side tying up <= C c a contract rests X x the size (first fit)",
    "--series-tier": "SERIES:SIDE:C:X -- a series' own tier list (restate the low tiers!)",
    "--side-cap": "SERIES or ALL:C:N -- a side tying up >= C c a contract rests N (0 = off: only a closing order)",
    "--twin-block": "N -- in a D/R pair, when one leg holds >= N, the other leg's same-bet side is off (0)",
    "--pin": "TICKER:N[:flat] -- flat N a side, no multipliers",
    "--pin-series": "SERIES:N[:flat] -- --pin for a whole series",
    "--pause-at": "SERIES:C -- markets at C c+ quote nothing (or only their unwinding side)",
    "--wave-added": "the wave's creation time, written on its entries (UNWIND_ONLY_WAVES covers only waves added before it)",
}

# Every tune lever for the report's parameter table (Jack 2026-11-08: "make sure to
# provide a table of the presets of all the parameters to tune, be thorough"):
# (flag, takes a value, what it is when not set, who decides). "your yes" = only on
# Jack's explicit word; "you" = from the tune menu.
LEVERS = [
    ("--price-min", True, "3c (the bot's 3-97c band)", "you"),
    ("--price-max", True, "97c", "you"),
    ("--room", True, "0: the scan's limits as listed", "you"),
    ("--follow-book", False, "off: the limits stand once a market trades", "you"),
    ("--reanchor", True, "off: the limits stay at the scan's", "you"),
    ("--reanchor-step", True, "5c (the tune's default)", "you; never 0"),
    ("--reanchor-jump", True, "10c (the tune's default)", "you; never 0"),
    ("--cushion", True, "the bot's own: dead 2c, live 5c off the last trade", "you"),
    ("--flip-through", False, "off: the reducing side rests its own size", "you"),
    ("--bank-size", True, "off: no bank-early", "you"),
    ("--bank-max", True, "none", "you"),
    ("--bank-by-hrs", True, "none", "you"),
    ("--bank-usd", True, "none", "you"),
    ("--no-drop", False, "off: a banked market drops back", "you"),
    ("--hold-max", False, "off", "you"),
    ("--cheap-tiers", True, "none: every side at the base size", "you to 5c; above 5c your yes"),
    ("--series-tier", True, "none", "your yes"),
    ("--size-mult", True, "x1", "your yes"),
    ("--side-cap", True, "none: sides tying up 90c+ quote like any other", "you"),
    ("--twin-block", True, "off: both legs of a D/R pair may hold the same bet", "you"),
    ("--pin", True, "none", "the pick-off watch adds these; removing one is your yes"),
    ("--pin-series", True, "none", "the pick-off watch adds these; removing one is your yes"),
    ("--pause-at", True, "none", "you"),
    ("--no-halts", False, "off: sweep breaker, tape trigger and toxic halts on", "your yes"),
]
REPEATABLE = {"--series-tier", "--side-cap", "--pin", "--pin-series", "--pause-at"}
# the bot's own guards on every wave market, read live from its startup lines
BOT_PARAM_LINES = [
    ("close clamp", "close clamp (2026-11-08):"),
    ("off-usual size", "off-usual size (2026-11-08):"),
    ("join wait", "join wait (2026-11-02):"),
    ("lone-ref guard, wave list", "lone-reference guard on the wave list (2026-11-08):"),
    ("lone-ref guard, rest of the bot", "lone-reference guard (2026-10-16):"),
    ("bait-fingerprint stop", "bait-fingerprint stop (2026-10-22):"),
    ("zero-size sides", "zero-size sides (2026-11-08):"),
    ("full-earn size", "full-earn size ("),
    ("sweep breaker", "event sweep breaker (2026-10-04):"),
    ("tape trigger", "tape trigger (2026-11-01):"),
    ("unwind-only waves scope", "unwind-only waves scope (2026-11-08):"),
    ("external marks (P&L today)", "external marks (2026-11-08):"),
    ("net halt", "net halt (2026-11-08):"),
]


def now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> dict:
    try:
        with open(REGISTRY, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"waves": {}}


def save(reg: dict) -> None:
    os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
    tmp = REGISTRY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, indent=1)
    os.replace(tmp, REGISTRY)


def port_free(p: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", p)) != 0


def next_port(reg: dict) -> int:
    taken = USED_PORTS | {int(w["port"]) for w in reg["waves"].values()}
    for p in range(8775, 8800):
        if p not in taken and port_free(p):
            return p
    raise SystemExit("no free dashboard port in 8775-8799")


def wave_dir(w: dict) -> str:
    return os.path.join(HERE, "run-logs", w["dir"])


def all_series(w: dict) -> list:
    return list(w.get("series") or []) + [s for s in (w.get("near") or []) if s not in (w.get("series") or [])]


def tune_cmd(name: str, w: dict, write: bool, group: str = "main") -> list:
    series, args = (w["near"], w.get("near_tune") or PRESETS["near"]) if group == "near" else (w["series"], w["tune"])
    cmd = [PY, os.path.join(HERE, "pvd_wave_tune.py"), "--series", ",".join(series),
           "--since", w["since"], *args,
           "--bank-url", f"http://127.0.0.1:{w['port']}/data",
           "--bank-hist", os.path.join(wave_dir(w), "history.json"),
           "--note", f"(wave {name}{' near' if group == 'near' else ''})"]
    if w.get("added"):
        # the bot's UNWIND_ONLY_WAVES covers only waves added before the switch
        # (Jack 2026-11-08: "shouldnt disqualify new waves from quoting")
        cmd += ["--wave-added", w["added"]]
    return cmd + (["--write"] if write else [])


def scan_cmd(w: dict, write: bool, group: str = "main") -> tuple:
    series = w["near"] if group == "near" else w["series"]
    cmd = [PY, os.path.join(HERE, "pvd_scan.py"), "--series", ",".join(series),
           "--no-vote-check", "--new-since", w["since"]]
    if w.get("stop_cap"):
        cmd += ["--stop-cap", w["stop_cap"]]
    if write:
        cmd.append("--write")
    env = dict(os.environ)
    if group == "near":
        ns = w.get("near_scan") or {"live": 80, "dead": 120, "decided": 300}
        env["PVD_SCAN_LIVE_SIZE"], env["PVD_SCAN_DEAD_SIZE"] = str(ns["live"]), str(ns["dead"])
        env["PVD_SCAN_DECIDED_SIZE"] = str(ns["decided"])
    else:
        env["PVD_SCAN_LIVE_SIZE"] = env["PVD_SCAN_DEAD_SIZE"] = str(int(w["scan_size"]))
    env["PVD_SCAN_BUDGET"] = str(int(GLOBAL_BUDGET + float(w["budget"])))
    # ...and the wave's OWN budget against its own live listings (2026-11-14: with $691k of
    # other waves' listings live, the shared rule alone listed 0 for a new wave)
    env["PVD_SCAN_OWN_BUDGET"] = str(int(float(w["budget"])))
    env["PVD_SCAN_OWN_SERIES"] = ",".join(all_series(w))
    # CHEAP SIDES AT THE WAVE'S SIZE (Jack 2026-11-11: "should quote larger size if <=5c,
    # otherwise if still under the $1.1 bar then yes dont quote it"): the scan judges a
    # market under the bar again with its cheap sides at this group's --cheap-tiers (the
    # sizes its tune rests them at); pinned series / tickers take no tiers. The NEAR group
    # has none.
    for k in ("PVD_SCAN_CHEAP_TIERS", "PVD_SCAN_TIER_SKIP", "PVD_SCAN_PRICE_MIN", "PVD_SCAN_PRICE_MAX",
              "PVD_SCAN_CUSHION", "PVD_SCAN_FOLLOW_BOOK"):
        env.pop(k, None)
    tune = (w.get("near_tune") or PRESETS["near"]) if group == "near" else (w.get("tune") or [])
    # ...and its quotable sides at the group's own band, cushion and follow-the-book
    for flag, key in (("--price-min", "PVD_SCAN_PRICE_MIN"), ("--price-max", "PVD_SCAN_PRICE_MAX"),
                      ("--cushion", "PVD_SCAN_CUSHION")):
        v = _vals(tune, flag)
        if v:
            env[key] = str(int(float(v[-1])))
    if "--follow-book" in tune:
        env["PVD_SCAN_FOLLOW_BOOK"] = "1"
    tiers = _vals(tune, "--cheap-tiers")
    if tiers:
        env["PVD_SCAN_CHEAP_TIERS"] = tiers[-1]
        skip = [v.split(":")[0] for v in _vals(tune, "--pin") + _vals(tune, "--pin-series")]
        if skip:
            env["PVD_SCAN_TIER_SKIP"] = ",".join(skip)
    return cmd, env


class PassLockBusy(Exception):
    pass


# PASS LOCK (2026-11-14, Jack "Yes, do them" on the market-wave skill update): a pass
# reads a wave's args, scans for minutes, then tunes -- a setarg landing in between was
# written back over by the old args (w1113-fxmetals 04:02Z 11/14: the 04:00Z pass
# reverted the first size-up; 11/12 13:02Z a by-hand retune just missed the 8 AM pass).
# One lock file per wave, held from reading its args through its tune's write; setarg /
# set / stop wait for it, the task's pass skips a wave another pass holds, a by-hand
# `run --only` waits. Same mtime staleness as scan.lock (the pass touches it per step).
PASS_LOCK_STALE_S = 1500


def pass_lock_path(name: str) -> str:
    # beside the registry it guards (run-logs/pvd-scan; a test's temp registry keeps its locks)
    return os.path.join(os.path.dirname(REGISTRY), f"wave_pass_{re.sub(r'[^A-Za-z0-9_.-]', '_', name)}.lock")


def _take_pass_lock(p: str, why: str) -> bool:
    for _ in range(2):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(p) < PASS_LOCK_STALE_S:
                    return False
                os.remove(p)                # stale: its pass died
            except OSError:
                return False
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(f"{os.getpid()} {now_z()} {why}\n")
        return True
    return False


class wave_pass_lock:
    """with wave_pass_lock(name, wait_s, why): ... -- raises PassLockBusy after wait_s."""

    def __init__(self, name: str, wait_s: float, why: str):
        self.p, self.wait_s, self.why, self.waited = pass_lock_path(name), float(wait_s), why, 0.0
        self.name = name

    def __enter__(self):
        os.makedirs(os.path.dirname(self.p), exist_ok=True)
        t0, said = time.time(), False
        while not _take_pass_lock(self.p, self.why):
            if time.time() - t0 >= self.wait_s:
                raise PassLockBusy(f"another pass of {self.name} holds {os.path.basename(self.p)}")
            if not said:
                print(f"waiting for the running pass of {self.name} ({os.path.basename(self.p)}) ...")
                said = True
            time.sleep(3)
        self.waited = time.time() - t0
        return self

    def touch(self):
        try:
            os.utime(self.p, None)
        except OSError:
            pass

    def __exit__(self, *exc):
        try:
            os.remove(self.p)
        except OSError:
            pass
        return False


def run_one(cmd: list, env=None, tail: int = 4) -> int:
    r = subprocess.run(cmd, cwd=HERE, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
    for ln in lines[-tail:]:
        print("   ", ln[:400])
    return r.returncode


def cmd_add(a) -> int:
    reg = load()
    if a.name in reg["waves"] and not a.replace:
        raise SystemExit(f"wave {a.name} exists (use --replace)")
    series = [s.strip() for s in (a.series or "").split(",") if s.strip()]
    if not series and not (a.near or "").strip():
        raise SystemExit("give --series and/or --near")
    clash = {s: n for n, w in reg["waves"].items() if n != a.name for s in all_series(w)
             if s in series or s in (a.near or "").split(",")}
    if clash:
        raise SystemExit(f"series already in another wave: {clash}")
    tune = list(PRESETS[a.preset]) + (shlex.split(a.tune_extra) if a.tune_extra else [])
    # AUTO MODE (the overnight check; nobody watching): the pick-off gates must
    # all pass, or nothing is written (Jack 2026-11-08: "Ensure this doesn't happen again")
    if a.auto:
        why = []
        if a.preset != "default":
            why.append(f"preset {a.preset}: auto mode lists on the default preset only")
        if float(a.budget) > AUTO_BUDGET_MAX:
            why.append(f"budget {a.budget:,.0f} > {AUTO_BUDGET_MAX:,.0f}")
        bad = [f for f in AUTO_FORBIDDEN if f in tune]
        if bad:
            why.append(f"{', '.join(bad)}: Jack's word only")
        if not a.detect:
            why.append("--detect is required (the detector's verdicts and event counts)")
            det = None
        else:
            with open(a.detect, encoding="utf-8") as fh:
                det = json.load(fh)
        near_l = [s.strip() for s in (a.near or "").split(",") if s.strip()]
        probe = {"series": series, "near": near_l, "tune": tune, "near_tune": list(PRESETS["near"]),
                 "budget": a.budget}
        checks = guard_checks() + wave_checks(a.name, probe, auto=True) + \
            capacity_checks([(a.name, probe)], auto=True, detect=det)
        if det is not None:
            checks += scope_checks(series, near_l, det, auto=True)
        n_fail = print_checks(checks)
        if why or n_fail:
            for x in why:
                print(f"FAIL  {'auto mode':34} {x}")
            print(f"NOT ADDED: {a.name} -- {len(why) + n_fail} failed check(s); nothing written")
            return 1
    # NEAR series (settle in 1-14 days; Jack 2026-11-07: "NEAR ... that was near but
    # quotable"): listed like the rest, pinned flat at the base (no tiers / multipliers)
    near = [s.strip() for s in (a.near or "").split(",") if s.strip() and s.strip() not in series]
    w = {"series": series, "since": a.since, "go_live": a.go_live or now_z(),
         "port": a.port or next_port(reg), "dir": f"pvd-watch-{a.name}", "scan_size": a.scan_size,
         "budget": a.budget, "stop_cap": a.stop_cap, "preset": a.preset, "tune": tune, "near": near,
         "near_tune": list(PRESETS["near"]), "near_scan": {"live": a.near_live, "dead": a.near_dead, "decided": 300},
         "enabled": True, "added": now_z(), "note": a.note or ""}
    reg["waves"][a.name] = w
    save(reg)
    print(f"added wave {a.name}: {len(series)} series, dashboard :{w['port']}, preset {a.preset}")
    return 0


def _run_wave(name: str, w: dict, a, lk=None) -> int:
    rc = 0
    for group in ("main", "near"):
        if not (w.get("near") if group == "near" else w.get("series")):
            continue
        print(f"== wave {name} {group} {now_z()}: scan ({'write' if a.write else 'dry run'})")
        cmd, env = scan_cmd(w, a.write, group)
        rc |= run_one(cmd, env, tail=3)
        if lk:
            lk.touch()
        if group == "main" and w.get("fair_cmd"):
            # FAIR REFRESH right before the tune (Jack 2026-11-13: "can you use the 24/7
            # prices, to build fair value?"): w["fair_cmd"] = [script, args...] in KL,
            # e.g. ["wave_fair.py", OUT, "--live"]; the tune's --fair-file reads OUT. A
            # failed refresh leaves the old file, which runs out at its valid_until
            # (then only floor sides rest), so its exit code never fails the pass
            print(f"== wave {name} {group}: fair refresh")
            if a.write:
                run_one([PY, os.path.join(HERE, w["fair_cmd"][0]), *w["fair_cmd"][1:]], tail=1)
            else:
                print("    (dry run: not refreshed)")
        if lk:
            lk.touch()
            # the args as they are NOW: a setarg made while this wave scanned waited for the
            # lock, so it can't land here -- but one made before it was taken is re-read
            w = load()["waves"].get(name) or w
        print(f"== wave {name} {group}: tune")
        rc |= run_one(tune_cmd(name, w, a.write, group), tail=1)
    return rc


def cmd_run(a) -> int:
    reg = load()
    rc = 0
    # a by-hand `run --only NAME` waits for a pass already running that wave; the task's
    # all-waves pass skips it (the other pass is doing the same job)
    wait = a.wait_lock if getattr(a, "wait_lock", None) is not None else (600.0 if a.only else 0.0)
    for name, w in reg["waves"].items():
        if a.only and name != a.only:
            continue
        if not w.get("enabled", True):
            print(f"== wave {name}: disabled")
            continue
        if not a.write:
            rc |= _run_wave(name, w, a)
            continue
        try:
            with wave_pass_lock(name, wait, "run --write") as lk:
                w = load()["waves"].get(name) or w      # the args as they are once the lock is ours
                if not w.get("enabled", True):
                    print(f"== wave {name}: disabled")
                    continue
                rc |= _run_wave(name, w, a, lk)
        except PassLockBusy:
            print(f"== wave {name} {now_z()}: another pass is running it ({os.path.basename(pass_lock_path(name))}) "
                  f"-- skipped")
    if a.write:
        # the ticker-date fix (cmd_datefix) for what the scans just listed; its own process
        # (incentive_mm's import), its exit code never fails the pass
        print(f"== datefix {now_z()}")
        run_one([PY, os.path.abspath(__file__), "datefix", a.only or "all", "--write"], tail=4)
    return 0 if rc in (0, 2) else rc


# TICKER-DATE FIX (2026-11-13, w1113-fxmetals; Jack: "do it as part of market-wave skill").
# The bot reads an event ticker's date as YYMONDD (incentive_mm.parse_event_date). A series
# that writes DDMONYY[HHMM] -- KXGOLDEVENT-17NOV261100 is Nov 17 2026 11:00 ET -- reads as
# 2017-11-26, and the universe's ticker_cutoff_passed() drops every such market before
# selection: 27 of the wave's 807 listed markets were selected, the ones dated the 26th.
DDMONYY = re.compile(r"^(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d{4})?$")
_MON = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}


def misread_fixes(events: dict, parse_event_date, markets: dict, existing: dict, now: datetime) -> tuple:
    """({event: ISO} to write, [(event, why) to report]). events = {event: a listed ticker};
    markets = {ticker: Kalshi market}. The event's real time = the earliest future one of
    Kalshi's occurrence / expected expiration / close. Left alone: an event already in
    `existing`, or one the bot's own reading (parse_event_date) puts on that day (ET, +-1).
    FIXED: a DDMONYY[HHMM] segment whose day-first reading IS that day while the bot's
    reading is another -- in the past (the market is dropped before selection) or in the
    future (KXUSDCADEVENT-30NOV260930 reads 2030-11-26: quoted past the real event once a
    wave's stop no longer covers it). The entry is the real time; the bot's cutoff becomes
    it minus its 10-min override buffer. REPORTED, not fixed: a misread into the past that
    isn't DDMONYY, or whose Kalshi time is not its DDMONYY day."""
    out, skip = {}, []
    for ev, t in sorted(events.items()):
        if ev in existing:
            continue
        d = parse_event_date(ev)
        past = d is not None and d < now - timedelta(days=1)
        m = markets.get(t) or {}
        times = []
        if m.get("status") in ("active", "open"):
            for k in ("occurrence_datetime", "expected_expiration_time", "close_time"):
                if m.get(k):
                    try:
                        x = datetime.fromisoformat(str(m[k]).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    if x > now:
                        times.append(x)
        if not times:
            if past:
                skip.append((ev, f"misread ({d:%Y-%m-%d}), market {m.get('status') or 'not read'}"))
            continue
        tm = min(times)
        k_day = (tm - timedelta(hours=5)).date()
        if d is not None and abs(((d - timedelta(hours=5)).date() - k_day).days) <= 1:
            continue                            # the bot reads it right
        seg = ev.split("-")[1] if "-" in ev else ""
        mm = DDMONYY.match(seg)
        if mm:
            try:
                dd_day = datetime(2000 + int(mm.group(3)), _MON[mm.group(2)], int(mm.group(1))).date()
            except ValueError:
                dd_day = None
            if dd_day is not None and abs((dd_day - k_day).days) <= 1:
                out[ev] = tm.strftime("%Y-%m-%dT%H:%M:%SZ")
                continue
        if past:
            skip.append((ev, f"misread ({d:%Y-%m-%d}); Kalshi's time {tm:%Y-%m-%d %H:%MZ} is not its "
                             f"{'DDMONYY day' if mm else 'DDMONYY reading (not DDMONYY)'}: check by hand"))
    return out, skip


def cmd_datefix(a) -> int:
    """Write event_start_overrides.json entries for every enabled wave's live listed events
    the bot misreads (misread_fixes). Hot-reloaded by the bot at its next universe refresh,
    no restart. Exit 3 = entries written."""
    import incentive_mm as imm                  # noqa: E402  (reads only)
    import imm_earnings_overrides as eo         # its atomic load_file / write_file
    from kalshi_reads import kalshi_get
    reg = load()
    now = datetime.now(timezone.utc)
    series = {s for n, w in reg["waves"].items() if w.get("enabled", True) and a.name in (n, "all")
              for s in all_series(w)}
    with open(LIST, encoding="utf-8") as fh:
        lst = json.load(fh).get("markets") or {}
    events = {}
    for t, e in lst.items():
        if t.split("-")[0] not in series:
            continue
        try:
            if datetime.fromisoformat(str(e["stop"]).replace("Z", "+00:00")) <= now:
                continue
        except (KeyError, ValueError):
            continue
        events.setdefault(t.rsplit("-", 1)[0], t)
    existing = eo.load_file()
    want = {ev: t for ev, t in events.items() if ev not in existing}
    markets = {}
    tick = sorted(want.values())
    for i in range(0, len(tick), 100):
        for m in (kalshi_get("/markets", {"tickers": ",".join(tick[i:i + 100]), "limit": 1000}) or {}).get("markets") or []:
            markets[m["ticker"]] = m
    fixes, skip = misread_fixes(want, imm.parse_event_date, markets, existing, now)
    print(f"datefix {a.name}: {len(events)} live listed events, {len(want)} without an override, "
          f"{len(fixes)} misread DDMONYY to fix, {len(skip)} to check by hand")
    for ev, why in skip[:10]:
        print(f"    skip {ev}: {why}")
    if fixes and a.write:
        cur = eo.load_file()                    # re-read: the earnings task may have written
        cur.update({ev: iso for ev, iso in fixes.items() if ev not in cur})
        eo.write_file(cur)
        print(f"    WROTE {len(fixes)} event-start override(s), e.g. "
              + ", ".join(f"{ev}={iso}" for ev, iso in list(fixes.items())[:3]))
        return 3
    return 0


def start_dash(name: str, w: dict) -> str:
    if not port_free(int(w["port"])):
        return f":{w['port']} already listening"
    d = wave_dir(w)
    os.makedirs(d, exist_ok=True)
    env = dict(os.environ)
    env.update({"PVD_WATCH_GO_LIVE": w["go_live"], "PVD_WATCH_DIR": d,
                "PVD_WATCH_SERIES": ",".join(all_series(w))})
    env.pop("PVD_WATCH_EXCLUDE", None)
    tok = os.path.join(HERE, "run-logs", "pvd-scan", "dash_token.txt")
    if os.path.exists(tok):             # phone access on the home network
        env["PVD_WATCH_TOKEN"] = open(tok, encoding="utf-8").read().strip()
        env["PVD_WATCH_HOST"] = "0.0.0.0"
    out = open(os.path.join(d, "watch.out.log"), "a")
    err = open(os.path.join(d, "watch.err.log"), "a")
    flags = 0x00000008 | 0x00000200 | 0x08000000          # DETACHED, NEW_PROCESS_GROUP, NO_WINDOW
    try:   # leave the caller's job (a tool shell) so the dashboard outlives it
        p = subprocess.Popen([PY, os.path.join(HERE, "imm_pvd_watch.py"), "--port", str(w["port"])],
                             cwd=HERE, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                             creationflags=flags | 0x01000000, close_fds=True)
    except OSError:
        p = subprocess.Popen([PY, os.path.join(HERE, "imm_pvd_watch.py"), "--port", str(w["port"])],
                             cwd=HERE, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                             creationflags=flags, close_fds=True)
    return f":{w['port']} started pid {p.pid}"


def cmd_dash(a) -> int:
    reg = load()
    for name, w in reg["waves"].items():
        if a.name in (name, "all"):
            print(f"{name}: {start_dash(name, w)}")
    return 0


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return (json.load(r) or {}).get("data")
    except Exception:  # noqa: BLE001
        return None


def args_table(tune: list) -> list:
    """[(flag, value)] from a tune arg list."""
    out, i = [], 0
    while i < len(tune):
        f = tune[i]
        if i + 1 < len(tune) and not tune[i + 1].startswith("--"):
            out.append((f, tune[i + 1]))
            i += 2
        else:
            out.append((f, "on"))
            i += 1
    return out


def param_table(name: str, w: dict) -> list:
    """The report's parameter table: every scope setting, every tune lever of each
    group (set or not, with what it is when not set), the bot's guards from its
    startup lines and the switch files -- now, what it does, how to change it,
    who decides."""
    rows = []
    r = rows.append

    def esc(x) -> str:
        return str(x).replace("|", "/").replace("\n", " ")
    ns = w.get("near_scan") or {}
    series = w.get("series") or []
    near = w.get("near") or []
    r("## Every parameter: now, what it does, how to change it, who decides")
    r("")
    r(f"Changes go through the registry so relights keep them, then `python pvd_waves.py run --only {name} "
      "--write`. who: \"your yes\" = only on Jack's explicit word; \"you\" = from the tune menu; "
      "\"auto\" = the skill or the relight task sets it.")
    r("")
    r("### Scope and listing")
    r("| parameter | now | what it does | change with | who |")
    r("|---|---|---|---|---|")
    r(f"| series (main group) | {len(series)}: {esc(', '.join(series) or 'none')} | quoted on the wave's preset | "
      f"`set {name} series A,B` | you (auto adds detector FIT series only) |")
    r(f"| NEAR group | {esc(', '.join(near) or 'none')} | settle in 1-14 days: dead {ns.get('dead', 120)} / live "
      f"{ns.get('live', 80)} a side ({ns.get('decided', 300)} on a decided side), the bot's cushions, 3-97c, no "
      f"multipliers or bank-early | `set {name} near A,B` | you |")
    r(f"| programs since | {w.get('since')} | the program-start floor the scan and tune work from | "
      f"`set {name} since ISO` | auto (30 min before the first new program) |")
    r(f"| stop cap | {w.get('stop_cap') or 'none: each program end'} | quoting stops here even if a program runs on | "
      f"`set {name} stop_cap ISO` | you |")
    r(f"| listing size | {w.get('scan_size')} a side | the size the scan lists at and judges the bar with | "
      f"`set {name} scan_size N` | you |")
    r("| scan bar | $1.10 by the stop | a market lists only if it projects $1.10+ by the stop at the listing size | "
      "env PVD_SCAN_BAR | you |")
    _room = scan_budget_room(w)
    r(f"| budget | ${float(w.get('budget') or 0):,.0f} of its own listings (now ${_room['live_own']:,.0f}), or the "
      f"shared ${GLOBAL_BUDGET:,.0f} + it across every wave (now ${_room['live_all']:,.0f}); room "
      f"${max(_room['room'], 0):,.0f} | caps new collateral, counted at the listing size (multiplied sides make the "
      f"real exposure larger) | `set {name} budget N` | you; over +$100k your yes (auto: +$50k at most) |")
    r(f"| enabled | {'yes' if w.get('enabled', True) else 'no'} | the relight task's pass 7 rescans and retunes "
      f"enabled waves only | `pvd_waves.py stop {name}` / `set {name} enabled true` | you; auto stops a wave that "
      f"didn't go live |")
    r(f"| dashboard | :{w.get('port')} | the wave's own dashboard | `pvd_waves.py dash {name}` | auto |")
    r(f"| wave added | {w.get('added') or 'unknown'} | stamped on each entry: UNWIND_ONLY_WAVES covers only the "
      f"waves added before the switch | - | auto |")
    r("")
    for group, tune in (("main", w.get("tune") or []), ("near", w.get("near_tune") or [])):
        if (group == "near" and not near) or (group == "main" and not series):
            continue
        r(f"### Tune levers, {group} group" + (f" (preset `{w.get('preset')}`)" if group == "main" else ""))
        r("| lever | now | what it does | change with | who |")
        r("|---|---|---|---|---|")
        g = " --group near" if group == "near" else ""
        for flag, val, absent, who in LEVERS:
            if val:
                vs = _vals(tune, flag)
                now = ", ".join(vs) if vs else f"not set: {absent}"
                how = f"`setarg {name} {flag[2:]} V{g}`" + (
                    " (repeatable: `--unset V` removes one)" if flag in REPEATABLE else "")
            else:
                now = "on" if flag in tune else f"not set: {absent}"
                how = f"`setarg {name} {flag[2:]}{g}` / `--unset`"
            r(f"| `{flag}` | {esc(now)} | {esc(ARG_NOTES.get(flag, ''))} | {how} | {who} |")
        extra = [f for f, _v in args_table(tune) if f not in {x[0] for x in LEVERS}]
        for f in extra:
            r(f"| `{f}` | {esc(', '.join(_vals(tune, f)) or 'on')} | {esc(ARG_NOTES.get(f, ''))} | "
              f"`setarg {name} {f[2:]}{g}` | you |")
        r("")
    r("### The bot's guards on every wave market (live, from its startup lines)")
    r("| guard | now | change with | who |")
    r("|---|---|---|---|")
    got = bot_lines([m for _n, m in BOT_PARAM_LINES])
    for nm, mk in BOT_PARAM_LINES:
        ln = got.get(mk)
        now = ln.split(mk, 1)[1].strip()[:280] if ln else "not in the bot log"
        r(f"| {nm} | {esc(now)} | launcher env + a restart | your yes |")
    env = launcher_env()
    for k, label in (("IMM_DAILY_LOSS_LIMIT", "daily-loss halt ($ of P&L today)"),
                     ("IMM_NET_LOSS_LIMIT", "net halt ($; 0 = off)"),
                     ("IMM_COLLATERAL_BUDGET", "collateral budget ($, the whole bot)")):
        r(f"| {label} | {esc(env.get(k, 'the code default'))} | launcher env + a restart | your yes |")
    for fn in ("HALT", "UNWIND_ONLY", "UNWIND_ONLY_EXCEPT_WAVES", "UNWIND_ONLY_WAVES"):
        r(f"| switch {fn} | {'ON' if os.path.exists(os.path.join(INC, fn)) else 'off'} | the file in "
          f"run-logs/incentive-mm | yours alone |")
    r("")
    return rows


# ---- the report by email (Jack 2026-11-08: "send me a test email report for
# wave-tool") -------------------------------------------------------------------
# `report NAME --email` sends the same report through the bot's alert mailer --
# the Gmail sender and recipient the daily emails use (send_cheap_report's
# imm.Alerter) -- the markdown as the plain part and as HTML; `--test` puts
# [TEST] in the subject and a banner on top. Nothing sends it on its own.
_CODE_CSS = ("font-family:Consolas,Menlo,monospace;font-size:12px;background:#f3f4f6;"
             "padding:0 3px;border-radius:3px")
_TH_CSS = "border:1px solid #d1d5db;background:#f3f4f6;padding:4px 6px;text-align:left;vertical-align:top"
_TD_CSS = "border:1px solid #e5e7eb;padding:4px 6px;vertical-align:top"
_STATUS_CSS = {"PASS": "color:#15803d;font-weight:600", "INFO": "color:#1d4ed8;font-weight:600",
               "WARN": "color:#b45309;font-weight:600", "FAIL": "color:#b91c1c;font-weight:700"}


def _md_inline(s: str) -> str:
    import html as _h
    s = _h.escape(s, quote=False)
    s = re.sub(r"`([^`]+)`", lambda m: f'<code style="{_CODE_CSS}">{m.group(1)}</code>', s)
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)


def _md_table(rows) -> str:
    def cells(r):
        r = r.strip()
        r = r[1:] if r.startswith("|") else r
        r = r[:-1] if r.endswith("|") else r
        return [c.strip() for c in r.split("|")]
    head = cells(rows[0])
    body = [cells(r) for r in rows[1:] if not re.match(r"^\|?\s*:?-{3,}", r.strip())]

    def td(c):
        css = _TD_CSS + (";" + _STATUS_CSS[c] if c in _STATUS_CSS else "")
        return f'<td style="{css}">{_md_inline(c)}</td>'
    th = "".join(f'<th style="{_TH_CSS}">{_md_inline(c)}</th>' for c in head)
    trs = "".join("<tr>" + "".join(td(c) for c in r) + "</tr>" for r in body)
    return (f'<table style="border-collapse:collapse;margin:6px 0 12px 0;font-size:12px">'
            f"<tr>{th}</tr>{trs}</table>")


def md_to_html(text: str, banner: str = "") -> str:
    """The report's markdown -- # to #### headings, `- ` and `1. ` items (two
    spaces a level), pipe tables, **bold**, `code` -- as email HTML with inline
    styles (mail clients drop <style> blocks); PASS / INFO / WARN / FAIL cells
    coloured."""
    out = []
    if banner:
        out.append('<div style="background:#fef3c7;border:1px solid #f59e0b;padding:6px 10px;'
                   f'margin:0 0 10px 0;border-radius:4px">{_md_inline(banner)}</div>')
    lines = text.split("\n")
    i, n, lst = 0, len(lines), None
    while i < n:
        ln = lines[i]
        st = ln.strip()
        if st.startswith("|"):
            if lst:
                out.append(f"</{lst}>")
                lst = None
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i])
                i += 1
            out.append(_md_table(rows))
            continue
        item = re.match(r"^( *)(- |\d+\. )(.*)$", ln)
        if item:
            kind = "ul" if item.group(2) == "- " else "ol"
            if lst != kind:
                if lst:
                    out.append(f"</{lst}>")
                out.append(f'<{kind} style="margin:4px 0 8px 0;padding-left:22px">')
                lst = kind
            pad = 16 * (len(item.group(1)) // 2)
            out.append(f'<li style="margin:2px 0' + (f';margin-left:{pad}px' if pad else '')
                       + f'">{_md_inline(item.group(3))}</li>')
            i += 1
            continue
        if lst:
            out.append(f"</{lst}>")
            lst = None
        h = re.match(r"^(#{1,4}) (.*)$", st)
        if h:
            lvl = len(h.group(1))
            size = {1: 20, 2: 16, 3: 14, 4: 13}[lvl]
            out.append(f'<h{lvl} style="font-size:{size}px;margin:16px 0 6px 0">'
                       f"{_md_inline(h.group(2))}</h{lvl}>")
        elif st:
            out.append(f'<p style="margin:4px 0 8px 0">{_md_inline(st)}</p>')
        i += 1
    if lst:
        out.append(f"</{lst}>")
    return ('<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;font-size:14px;'
            'color:#111827;line-height:1.4">' + "\n".join(out) + "</body></html>")


def report_subject(name: str, summ: dict, now: datetime, test: bool = False) -> str:
    try:
        import pytz
        when = now.astimezone(pytz.timezone("America/New_York")).strftime("%m/%d %I:%M %p ET")
        when = when.replace(" 0", " ", 1)
    except Exception:  # noqa: BLE001
        when = now.strftime("%m/%d %H:%MZ")
    parts = [f"preflight {summ.get('preflight', '?')}"]
    if summ.get("quoted") is not None:
        parts += [f"quoting {summ['quoted']} of {summ.get('listed')}",
                  f"est ${float(summ.get('rate') or 0):,.0f}/day"]
    else:
        parts.append("dashboard down")
    if summ.get("pickoffs") is not None:
        k = int(summ["pickoffs"])
        parts.append(f"{k} pick-off{'' if k == 1 else 's'} flagged"
                     + (f" (${float(summ.get('pickoffs_usd') or 0):,.0f})" if k else ""))
    return f"{'[TEST] ' if test else ''}Wave {name} report {when}: " + ", ".join(parts)


def _report_alerter():
    """The bot's alert mailer, set up as the daily emails do (imm_quote_gaps loads
    the env and the alert credentials); its import chatter is kept off stdout."""
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        import imm_quote_gaps  # noqa: F401
        import incentive_mm as imm
        return imm.Alerter("WAVE", live=True)


def send_report_email(name: str, text: str, summ: dict, test: bool = False,
                      now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    subj = report_subject(name, summ, now, test)
    banner = ("TEST -- sent by hand: `pvd_waves.py report " + name + " --email --test`. "
              "Nothing sends this report on its own yet.") if test else ""
    alerter = _report_alerter()
    if not getattr(alerter, "enabled", False):
        print("report email: alert credentials not configured; nothing sent", file=sys.stderr)
        return 1
    ok = alerter.send_message(text, subject=subj, html=md_to_html(text, banner))
    print(f"report email {'sent' if ok else 'FAILED'}: {subj}")
    return 0 if ok else 1


def cmd_report(a) -> int:
    reg = load()
    w = reg["waves"].get(a.name)
    if not w:
        raise SystemExit(f"no wave {a.name}")
    now = datetime.now(timezone.utc)
    L = {}
    try:
        with open(LIST, encoding="utf-8") as fh:
            L = json.load(fh).get("markets") or {}
    except (OSError, ValueError):
        pass
    mine = {t: e for t, e in L.items() if t.split("-")[0] in set(all_series(w))}
    live = {t: e for t, e in mine.items() if e.get("stop") and
            datetime.fromisoformat(e["stop"].replace("Z", "+00:00")) > now}
    stops = sorted({e["stop"] for e in live.values()})
    sizes = {}
    for e in live.values():
        sizes[int(e.get("size") or 0)] = sizes.get(int(e.get("size") or 0), 0) + 1
    capped = sum(1 for e in live.values() if "cap:" in (e.get("cheap") or ""))
    d = _get(f"http://127.0.0.1:{w['port']}/data")
    summ = {}                   # the email subject's numbers
    L_ = []
    p = L_.append
    p(f"# Wave `{a.name}` -- parameters, assumptions and state ({now:%Y-%m-%d %H:%MZ})")
    p("")
    if a.detect:
        try:
            with open(a.detect, encoding="utf-8") as fh:
                det = json.load(fh)
            p("## What lit and why these series")
            p(f"Detector: {det['programs']} new liquidity programs since {det['since']}. Bar: "
              f"{det['params']['min_markets']} markets or ${det['params']['min_pool']:,.0f}/day; FIT = settles "
              f">= {det['params']['min_days_out']:g} days out, not quoted / listed / member, category not in "
              f"{', '.join(det['params']['risky_categories'])}, not a mention series, not blocklisted; "
              f"NEAR = the same but settling in {det['params'].get('min_soon_days', 1):g}-"
              f"{det['params']['min_days_out']:g} days (listed, pinned at the base); SOON = within a day "
              f"(Jack's call).")
            p("")
            p("| verdict | series | markets | events | pool $/day | settles | why |")
            p("|---|---|---|---|---|---|---|")
            for r in det["series"]:
                if r["verdict"] in ("FIT", "NEAR", "SOON", "CHECK") or r["series"] in w["series"]:
                    p(f"| {r['verdict']} | {r['series']} | {r['markets']} | {r['events']} | {r['pool_day']:,.0f} | "
                      f"{(r['median_settle'] or '-')[:10]} | {'; '.join(r['why']) or r['category']} |")
            p("")
        except (OSError, ValueError, KeyError) as ex:
            p(f"(detector file unreadable: {ex})")
    p("## Listing")
    p(f"- Series ({len(w['series'])}): {', '.join(w['series'])}")
    if w.get("near"):
        ns = w.get("near_scan") or {}
        p(f"- NEAR group ({len(w['near'])}, settle in 1-14 days): {', '.join(w['near'])} -- the 11/04 midterm "
          f"treatment: dead {ns.get('dead', 120)} / live {ns.get('live', 80)} a side (decided side "
          f"{ns.get('decided', 300)}), the bot's kind cushions (dead 2c, live 5c), band 3-97c, no tiers / "
          f"multipliers / bank-early; tune args: {' '.join(w.get('near_tune') or [])}")
    p(f"- Programs since {w['since']}; go-live {w['go_live']}; listed now **{len(live)}** markets "
      f"(of {len(mine)} on the list), stops {', '.join(s[:16] for s in stops[:4])}{' ...' if len(stops) > 4 else ''}")
    p(f"- Scan: {w['scan_size']} a side at listing, bar $1.10 by the stop, budget ${float(w['budget']):,.0f} of its "
      f"own listings or the shared ${GLOBAL_BUDGET:,.0f} + it across every wave (PVD_SCAN_BUDGET), whichever has "
      f"more room; stop cap {w.get('stop_cap') or 'none (program end)'}")
    p(f"- Sizes now: {', '.join(f'{n} at {s}' for s, n in sorted(sizes.items()))}; {capped} markets with a 90c+ side capped")
    p("")
    L_.extend(param_table(a.name, w))
    p("## State")
    if d:
        t = d.get("totals") or {}
        summ.update(quoted=t.get("quoted"), listed=t.get("listed"), rate=t.get("rate", 0))
        p(f"- Dashboard http://127.0.0.1:{w['port']}/ read {str(d.get('now'))[:19]}Z")
        p(f"- Quoting {t.get('quoted')} of {t.get('listed')} listed, two-sided {t.get('two_sided')}; "
          f"est **${t.get('rate', 0):,.0f}/day**, rewards so far ${t.get('est', 0):,.2f}")
        p(f"- If every resting order filled: ${t.get('rest_coll', 0):,.0f}; positions ${t.get('cost', 0):,.0f} "
          f"({t.get('fills')} fills, open ${t.get('open', 0):,.0f}); shard-0 free cash ${t.get('cash0', 0):,.0f}")
        for b in (d.get("pace") or {}).get("buckets", []):
            p(f"  - pacing {b['label']}: {b['n']}")
    else:
        p(f"- Dashboard :{w['port']} has no data yet (first poll takes a few minutes).")
    p("")
    p("## Pick-off guards (preflight)")
    try:
        gc = guard_checks() + wave_checks(a.name, w)
        if a.detect:
            with open(a.detect, encoding="utf-8") as fh:
                gc += scope_checks(w.get("series") or [], w.get("near") or [], json.load(fh))
        n_f = sum(1 for c in gc if c[0] == FAIL)
        summ["preflight"] = "FAIL" if n_f else "PASS"
        p(f"**{'FAIL' if n_f else 'PASS'}** ({n_f} fail, {sum(1 for c in gc if c[0] == WARN)} warn)")
        p("")
        p("| status | check | detail |")
        p("|---|---|---|")
        for st_, nm_, dt_ in gc:
            if st_ != PASS or nm_.startswith(("join wait", "off-usual", "sweep", "tape", f"{a.name} main re-anchor")):
                p(f"| {st_} | {nm_} | {dt_.replace('|', '/')[:240]} |")
    except Exception as ex:  # noqa: BLE001
        p(f"(preflight failed: {ex})")
    p("")
    try:
        hrs = max(1.0, (now - datetime.fromisoformat(w["go_live"].replace("Z", "+00:00"))).total_seconds() / 3600.0)
        po = pickoff_scan(argparse.Namespace(name=a.name, hours=hrs, min_c=5.0, min_usd=25.0, series_trip=3), reg)
        summ.update(pickoffs=len(po["flagged"]), pickoffs_usd=po["flagged_usd"])
        p(f"## Pick-offs since go-live ({len(po['flagged'])} flagged, ${po['flagged_usd']:,.0f})")
        p(f"Adding fills marked to the book with our orders out; flagged at 5c+ and $25+ against us. "
          f"{po['wave_fills']} fills on the wave's markets.")
        for g in sorted(po["flagged"], key=lambda x: x["adverse_usd"], reverse=True)[:10]:
            p(f"- {g['ticker']} {g['side']} {g['contracts']:,.0f} @ {g['vwap_c']:.1f}c, mark {g['mark_c']:.1f}: "
              f"${g['adverse_usd']:,.0f} ({g['last'][:16]}Z)")
        if po["series_trips"]:
            p(f"- SERIES TRIPS: {', '.join(t_['series'] for t_ in po['series_trips'])} (3+ markets picked off)")
    except Exception as ex:  # noqa: BLE001
        p(f"(pick-off scan failed: {ex})")
    p("")
    p("## Assumptions to check")
    p("- Information risk: what moves these prices, and can someone see it before our resting order moves?")
    p("- Horizon: long-dated fills lock cash until settlement; near-term markets move on news (pin them at a base).")
    p("- The scan's $/day is optimistic (2028: scan $2.4k/day vs live ~$0.9k at 80 a side); size up after listing.")
    p("- Cheap sides scale weakly per contract (scored window = first 1,000 contracts from the best price); "
      "measured ~$4-7/day per extra $1k at risk.")
    p("")
    p("## Tune menu (say which)")
    p("1. Size: `--size-mult` (all sides), `--cheap-tiers` / `--series-tier` (by cost per contract), bank 200/300.")
    p("2. Breakers: `--no-halts` (removes the sweep breaker; wave breaker + 200+ tape trigger instead).")
    p("3. Cash: `--side-cap ALL:C:N` (no preset cap since 11/08: sides tying up 90c+ quote like any other; "
      "lower C = less cash on expensive sides, N = 0 is off), `--pause-at SERIES:C`.")
    p("Every lever's value now and its command are in the parameter table above.")
    p("4. Scope: drop series, pin near-term ones (`--pin-series S:300`), stop a market (list `stopped`).")
    p("5. Band / limits: `--price-min/--price-max`, `--reanchor`.")
    text = "\n".join(L_)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    if getattr(a, "email", False):
        return send_report_email(a.name, text, summ, test=a.test)
    return 0


REG_EDIT_WAIT_S = 900.0     # setarg / set / stop wait this long for a running pass of the wave


def _locked_edit(name: str, fn, why: str) -> int:
    """Run fn() holding the wave's pass lock (a pass mid-scan would write its old args
    back over the change). A wave with no registry entry needs no lock."""
    if name not in load()["waves"]:
        return fn()
    try:
        with wave_pass_lock(name, REG_EDIT_WAIT_S, why) as lk:
            if lk.waited >= 1:
                print(f"(waited {lk.waited:.0f} s for the running pass of {name})")
            return fn()
    except PassLockBusy as ex:
        raise SystemExit(f"{ex} for {REG_EDIT_WAIT_S / 60:.0f}+ min; nothing changed -- try again")


def cmd_setarg(a) -> int:
    return _locked_edit(a.name, lambda: _setarg(a), "setarg")


def _setarg(a) -> int:
    reg = load()
    w = reg["waves"].get(a.name)
    if not w:
        raise SystemExit(f"no wave {a.name}")
    a.flag = "--" + a.flag.lstrip("-")              # given without dashes: "cheap-tiers"
    key = "near_tune" if a.group == "near" else "tune"
    w.setdefault(key, list(PRESETS["near"]) if key == "near_tune" else [])
    pairs = args_table(w[key])
    if a.unset:     # with a VALUE, only that pair (one --pin of several); without, every one
        pairs = [(f, v) for f, v in pairs if not (f == a.flag and (a.value is None or v == a.value))]
    if not a.unset:
        repeatable = a.flag in ("--series-tier", "--pin", "--pin-series", "--side-cap", "--pause-at")
        if not repeatable:
            pairs = [(f, v) for f, v in pairs if f != a.flag]
        pairs.append((a.flag, a.value if a.value is not None else "on"))
    w[key] = [x for f, v in pairs for x in ([f] if v == "on" else [f, v])]
    save(reg)
    print(f"{a.name} {a.group}: {'unset' if a.unset else 'set'} {a.flag} {a.value or ''} -> {' '.join(w[key])}")
    return 0


def cmd_set(a) -> int:
    return _locked_edit(a.name, lambda: _set(a), "set")


def _set(a) -> int:
    reg = load()
    w = reg["waves"].get(a.name)
    if not w:
        raise SystemExit(f"no wave {a.name}")
    v = a.value
    if a.key in ("scan_size", "port"):
        v = int(v)
    elif a.key == "budget":
        v = float(v)
    elif a.key == "enabled":
        v = v.lower() in ("1", "true", "yes", "on")
    elif a.key in ("series", "near"):
        v = [s.strip() for s in v.split(",") if s.strip()]
    w[a.key] = v
    save(reg)
    print(f"{a.name}: {a.key} = {v}")
    return 0


def cmd_list(_a) -> int:
    for name, w in load()["waves"].items():
        print(f"{name}: {'on ' if w.get('enabled', True) else 'off'} :{w['port']} {len(w['series'])} series "
              f"+ {len(w.get('near') or [])} near since "
              f"{w['since']} preset {w['preset']} -- {', '.join(w['series'][:6])}{' ...' if len(w['series']) > 6 else ''}")
    return 0


def cmd_stop(a) -> int:
    """TURN A WAVE OFF (Jack 2026-11-07 23:15Z: "Turn off the wave you just greenlit"):
    disable it (no rescans, tunes or dashboard restarts) and move every live list
    entry of its series to the list's "stopped" (the bot stops quoting them at its
    next reload; the scanner leaves them alone until a LATER program period).
    Positions ride: an unlisted market is not wound down."""
    return _locked_edit(a.name, lambda: _stop(a), "stop")


def _stop(a) -> int:
    reg = load()
    w = reg["waves"].get(a.name)
    if not w:
        raise SystemExit(f"no wave {a.name}")
    lock = SCAN_LOCK
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    for _ in range(60):
        if not (os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 1500):
            break
        time.sleep(5)
    else:
        raise SystemExit("a scan holds run-logs/pvd-scan/scan.lock; try again in a minute")
    open(lock, "w").close()
    try:
        with open(LIST, encoding="utf-8") as fh:
            d = json.load(fh)
        L = d.setdefault("markets", {})
        st = d.setdefault("stopped", {})
        mine = set(all_series(w))
        now = now_z()
        moved = [t for t, e in L.items() if t.split("-")[0] in mine and (e.get("stop") or "") > now]
        for t in moved:
            st[t] = {"at": now, "reason": f"{a.reason} (pvd_waves.py stop {a.name})", "entry": L.pop(t)}
        if moved and not a.dry_run:
            with open(LIST + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
            os.replace(LIST + ".tmp", LIST)
    finally:
        os.remove(lock)
    if not a.dry_run:
        w["enabled"] = False
        w.setdefault("history", []).append(f"{now_z()} stopped: {a.reason}")
        save(reg)
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            pos = (json.load(fh) or {}).get("own_pos") or {}
    except (OSError, ValueError):
        pos = {}
    held = {t: p for t, p in pos.items() if t.split("-")[0] in set(all_series(w)) and abs(float(p)) >= 1}
    print(f"{'DRY RUN: would stop' if a.dry_run else 'stopped'} {a.name}: {len(moved)} live entries -> "
          f"'stopped', wave disabled; {len(held)} held position(s) ride"
          + (f" ({', '.join(f'{t} {p:+.0f}' for t, p in list(held.items())[:5])})" if held else ""))
    return 0


def cmd_remove(a) -> int:
    reg = load()
    reg["waves"].pop(a.name, None)
    save(reg)
    print(f"removed {a.name} from the registry (its list entries stay; stop them on the list)")
    return 0


# ---------------------------------------------------------------------------
# PREFLIGHT: the guards a wave relies on (Jack 2026-11-08 after the 11/07-11/08
# pick-offs: "Ensure this doesn't happen again", "the pickoffs yesterday were
# unacceptable"). Each check is (status, name, detail); FAIL blocks a listing.
# ---------------------------------------------------------------------------
PASS, WARN, FAIL, INFO = "PASS", "WARN", "FAIL", "INFO"
MIN_EVENTS = 5                      # Jack 11/08: a wave = 5+ events in one family
MIN_CASH0 = float(os.environ.get("PVD_MIN_CASH0", "5000"))
AUTO_BUDGET_MAX = 50000.0
AUTO_FORBIDDEN = ("--no-halts", "--size-mult", "--series-tier")
PROBE_LOT = 10              # a 90c+ side capped at 1-9 contracts is a probe (11/07)
# switch files in run-logs/incentive-mm: Jack's alone to create or remove
SWITCHES = [
    ("HALT", FAIL, "the bot is idle (HALT file): nothing quotes"),
]
# UNWIND_ONLY_WAVES (Jack 2026-11-08: "yes for existing waves, but shouldnt disqualify
# new waves from quoting"): INFO on a bot that scopes it to the waves listed before the
# switch (this startup line), else FAIL -- there a new wave would rest nothing
UNWIND_SCOPE_MARK = "unwind-only waves scope (2026-11-08):"
# the bot's startup lines (latest start) -> (marker, test, what it guards)
BOT_GUARDS = [
    ("join wait", "join wait (2026-11-02):",
     lambda ln: (": on --" in ln and "sub-penny" in ln and "h memory" in ln),
     "bait defense: a better price must stand 2 s per $ at risk before we join it, on penny AND 0.1c "
     "books, and a side back after a halt starts from its last rest (24 h memory)"),
    ("off-usual size", "off-usual size (2026-11-08):", lambda ln: ": on --" in ln,
     "a listed side resting 3c+ toward the taker past its 6 h median touch rests only base size "
     "(no usual price yet: base too)"),
    ("sweep breaker", "event sweep breaker (2026-10-04):", lambda ln: ": on --" in ln,
     "a fill that clears our order pulls the event 600 s (not on no_halts series)"),
    ("tape trigger", "tape trigger (2026-11-01):",
     lambda ln: re.search(r"\):\s*(on|ab)\b", ln) is not None,
     "a 100+ taker burst on the public tape pauses the event 60 s (200+ on no_halts series)"),
    ("bait-fingerprint stop", "bait-fingerprint stop (2026-10-22):", lambda ln: ": on --" in ln,
     "a whole-rung fill on a thin jumped top by a taker of exactly our size stops that side to ET midnight"),
    ("company KPIs off", "company KPIs (2026-11-08):", lambda ln: ": OFF --" in ln,
     "every Kalshi KPIs-tagged series frozen (Jack 11/08)"),
    ("close clamp", "close clamp (2026-11-08):", lambda ln: ": on --" in ln,
     "the side that reduces a position rests no more than 5c past its 6 h usual price, or at "
     "breakeven or better (Jack 11/08)"),
    ("lone-ref guard on waves", "lone-reference guard on the wave list (2026-11-08):",
     lambda ln: ": on --" in ln,
     "a listed wave market's side whose reference rests on a lone thin top our rung jumped to "
     "stands aside (Jack 11/08: \"turn on lone ref guard for market-wave\")"),
]
BOT_INFO = [
    ("lone-reference guard", "lone-reference guard (2026-10-16):"),
    ("external marks", "external marks (2026-11-08):"),
    ("net halt", "net halt (2026-11-08):"),
    ("full-earn size", "full-earn size ("),
    ("no-halts series", "post-vote dead books: no halts on"),
]
START_MARK = "[IMM] account balance $"
CYCLE_MARK = "mkts quoted"


def _tail_lines(path: str, max_bytes: int = 80_000_000) -> list:
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()
        data = fh.read()
    return data.decode("utf-8", "replace").splitlines()


def bot_lines(markers) -> dict:
    """{marker: the LAST bot-log line containing it}, newest two day logs."""
    logs = sorted(glob.glob(os.path.join(INC, "incentive-mm-20*.log")), key=os.path.getmtime, reverse=True)[:2]
    found: dict = {}
    for p in logs:
        try:
            lines = _tail_lines(p)
        except OSError:
            continue
        for ln in reversed(lines):
            for m in markers:
                if m not in found and m in ln:
                    found[m] = ln
            if len(found) == len(markers):
                return found
    return found


def _line_ts(ln: str):
    try:
        return datetime.strptime(ln[:20], "%Y-%m-%d %H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def launcher_env() -> dict:
    try:
        with open(LAUNCHER, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except OSError:
        return {}
    found = re.findall(r'\$ProbeEnv\s*=\s*"([^"]*)"', txt)
    env = {}
    for k, v in re.findall(r"set (IMM_[A-Z0-9_]+)=([^&]*)&&", max(found, key=len) if found else ""):
        env[k] = v.strip()
    return env


def guard_checks(now=None) -> list:
    """The bot-wide state every wave depends on."""
    now = now or datetime.now(timezone.utc)
    out = []
    for fn, st, why in SWITCHES:
        if os.path.exists(os.path.join(INC, fn)):
            out.append((st, f"switch {fn}", why))
    if os.path.exists(os.path.join(INC, "UNWIND_ONLY")):
        exc = os.path.exists(os.path.join(INC, "UNWIND_ONLY_EXCEPT_WAVES"))
        out.append((WARN if exc else FAIL, "switch UNWIND_ONLY",
                    "the rest of the bot only unwinds; waves quote (EXCEPT_WAVES)" if exc
                    else "the whole bot only unwinds: a new wave rests nothing"))
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            hu = float((json.load(fh) or {}).get("halted_until") or 0)
        if hu > now.timestamp():
            out.append((FAIL, "bot halted", f"a P&L / account halt until "
                        f"{datetime.fromtimestamp(hu, timezone.utc):%Y-%m-%d %H:%MZ}: nothing quotes"))
    except (OSError, ValueError):
        out.append((WARN, "bot state", "imm_state.json unreadable"))
    marks = [m for _n, m, _t, _w in BOT_GUARDS] + [m for _n, m in BOT_INFO] + [
        START_MARK, CYCLE_MARK, UNWIND_SCOPE_MARK]
    got = bot_lines(marks)
    uw = os.path.join(INC, "UNWIND_ONLY_WAVES")
    if os.path.exists(uw):
        if ": existing waves" in got.get(UNWIND_SCOPE_MARK, ""):
            try:
                when = f"{datetime.fromtimestamp(os.path.getmtime(uw), timezone.utc):%m/%d %H:%MZ}"
            except OSError:
                when = "the switch"
            out.append((INFO, "switch UNWIND_ONLY_WAVES",
                        f"held wave markets only unwind (since 85a5d8e at the outside price, never past usual "
                        f"+-5c, never leading); flat ones of a stamped wave quote (the waves listed before {when} "
                        f"stay dark until a tune stamps them)"))
        else:
            out.append((FAIL, "switch UNWIND_ONLY_WAVES",
                        "every wave market only unwinds, at breakeven or better: on this bot (no "
                        "'unwind-only waves scope ... existing waves' line) a NEW wave rests nothing. "
                        "Jack's switch -- never remove it without his word"))
    cyc = _line_ts(got.get(CYCLE_MARK, ""))
    if not cyc or (now - cyc).total_seconds() > 600:
        out.append((FAIL, "bot cycling", f"last 'mkts quoted' line {cyc:%H:%MZ}" if cyc
                    else "no 'mkts quoted' line in the bot log"))
    else:
        out.append((PASS, "bot cycling", got[CYCLE_MARK][21:140].strip()))
    start = _line_ts(got.get(START_MARK, ""))
    for name, mk, test, why in BOT_GUARDS:
        ln = got.get(mk)
        if not ln:
            out.append((FAIL, name, f"no startup line '{mk}' in the bot log -- guard not confirmed; {why}"))
            continue
        lt = _line_ts(ln)
        stale = bool(start and lt and (start - lt).total_seconds() > 300)
        ok = test(ln)
        out.append((PASS if ok and not stale else WARN if ok else FAIL, name,
                    ("(line older than the latest start) " if stale else "") + why
                    + ("" if ok else f" -- the bot says: {ln[21:200].strip()}")))
    for name, mk in BOT_INFO:
        ln = got.get(mk)
        if ln:
            txt = ln.split(mk, 1)[1].strip()
            if name == "no-halts series":
                n = re.match(r"(\d+)", txt)
                txt = (f"{n.group(1)} series quote with NO sweep breaker / toxic halts (wave breaker + 200+ tape "
                       f"trigger instead): {txt[:160]}") if n else txt[:200]
            out.append((INFO, name, txt[:220]))
    # shard-0 free cash funds the whole bot and every wave: fills lock it to settlement
    # (the w1108 auto run listed first and found $1.5k after -- check it BEFORE an add)
    try:
        sys.path.insert(0, HERE)
        from kalshi_reads import kalshi_get
        b = kalshi_get("/portfolio/balance") or {}
        cash0 = None
        for row in b.get("balance_breakdown") or []:
            if str(row.get("exchange_index")) == "0":
                cash0 = float(row.get("balance"))
        if cash0 is None and b.get("balance_dollars") is not None:
            cash0 = float(b["balance_dollars"])
        if cash0 is None:
            out.append((WARN, "shard-0 cash", "balance read gave no shard-0 figure"))
        else:
            out.append((PASS if cash0 >= MIN_CASH0 else FAIL, "shard-0 cash",
                        f"${cash0:,.0f} free (a wave needs ${MIN_CASH0:,.0f}+; only Jack moves money)"))
    except Exception as ex:  # noqa: BLE001
        out.append((WARN, "shard-0 cash", f"balance unreadable ({str(ex)[:80]})"))
    env = launcher_env()
    if env.get("IMM_WAVE_BREAKER", "on").lower() == "off":
        out.append((FAIL, "wave breaker", "IMM_WAVE_BREAKER=off in the launcher"))
    if env.get("IMM_DAILY_LOSS_LIMIT"):
        out.append((INFO, "daily-loss halt", f"-${float(env['IMM_DAILY_LOSS_LIMIT']):,.0f} on P&L today "
                    "(marked without our own orders or fills since 0393404: real prices)"))
    return out


# ---------------------------------------------------------------------------
# CAPACITY (2026-11-14, Jack "Yes, do them" on the market-wave skill update): what the
# 11/13-11/14 waves ran into after they listed -- metals and crypto fills draw on SHARD 2
# ($1,140 free there, preflight checked shard 0 only); the shared scan budget ($691k of
# other waves' listings live: a new wave listed 0); the resting-order cap (7,128 of
# 8,000 after the FX listing) and the 8,000-order read that doubled 3,462 sides once the
# cap went to 12,000; the collateral budget (150 markets refused at $1M); restarts (8 in
# one night, each re-queues the book and reads ~15 min low). FAIL in auto mode only where
# a listing would rest nothing or blind the bot; WARN / INFO otherwise.
# ---------------------------------------------------------------------------
MIN_CASH_OTHER = float(os.environ.get("PVD_MIN_CASH_OTHER", "1000"))   # shards 1-3
RESTING_ROOM_FAIL = 500           # resting orders left under IMM_MAX_TOTAL_RESTING
RESTING_ROOM_WARN = 2000
RESTART_WINDOW_MIN = 30.0
READ_LOW_MIN = 15.0               # reads within this long of a restart run low
UNIVERSE_MARK = "] universe: "
TRUNC_MARK = "RESTING_READ_TRUNCATED"
PLACE_CAP_MARK = "placement cap "


def scan_budget_room(w: dict, L: "dict | None" = None, now: "datetime | None" = None) -> dict:
    """The listing room a wave's next scan has: the shared rule (every wave's live scan
    adds against PVD_SCAN_BUDGET + its budget) or its own (its own live adds against its
    budget), whichever is larger -- pvd_scan's pick_within_budget."""
    now = now or datetime.now(timezone.utc)
    if L is None:
        try:
            with open(LIST, encoding="utf-8") as fh:
                L = json.load(fh).get("markets") or {}
        except (OSError, ValueError):
            L = {}
    mine = tuple(all_series(w))
    nts = now.timestamp()
    live_all = live_own = 0.0
    for t, e in L.items():
        if not str(e.get("added", "")).startswith("scan 20"):
            continue
        st = _ts_of(e.get("stop"))
        if st is None or st <= nts:
            continue
        c = float(e.get("coll_usd") or 0)
        live_all += c
        if mine and t.split("-")[0].startswith(mine):
            live_own += c
    wb = float(w.get("budget") or 0)
    shared = GLOBAL_BUDGET + wb - live_all
    own = wb - live_own
    return {"live_all": live_all, "live_own": live_own, "budget": wb, "shared_room": shared,
            "own_room": own, "room": max(shared, own)}


def _shards_of(tickers) -> dict:
    """{exchange_index: [series]} for one sample market per series (signed reads)."""
    sys.path.insert(0, HERE)
    from kalshi_reads import kalshi_get
    out: dict = {}
    for t in tickers:
        try:
            m = (kalshi_get(f"/markets/{t}") or {}).get("market") or {}
        except Exception:  # noqa: BLE001
            continue
        if m.get("exchange_index") is not None:
            out.setdefault(str(m["exchange_index"]), []).append(t.split("-")[0])
    return out


def _sample_tickers(series: list, detect=None) -> list:
    """One market per series: a live list entry, else (a new wave) one of the detector's
    events read from Kalshi."""
    try:
        with open(LIST, encoding="utf-8") as fh:
            L = json.load(fh).get("markets") or {}
    except (OSError, ValueError):
        L = {}
    out, want = [], list(dict.fromkeys(series))
    have = {}
    for t in L:
        s = t.split("-")[0]
        if s in want and s not in have:
            have[s] = t
    for s in want:
        if s in have:
            out.append(have[s])
            continue
        row = next((r for r in (detect or {}).get("series", []) if r.get("series") == s), None)
        evs = [x.get("event") for x in (row or {}).get("event_list") or [] if x.get("event")]
        if not evs:
            continue
        try:
            sys.path.insert(0, HERE)
            from kalshi_reads import kalshi_get
            ms = (kalshi_get("/markets", {"event_ticker": evs[0], "limit": 1}) or {}).get("markets") or []
            if ms:
                out.append(ms[0]["ticker"])
        except Exception:  # noqa: BLE001
            continue
    return out


def capacity_checks(waves: list, auto: bool = False, detect=None, now=None) -> list:
    """waves = [(name, w)] (a probe for a new wave works too). Bot-wide capacity once,
    then each wave's scan-budget room and the cash on the shards its markets trade on."""
    now = now or datetime.now(timezone.utc)
    nts = now.timestamp()
    out = []
    env = launcher_env()
    got = bot_lines([CYCLE_MARK, UNIVERSE_MARK, TRUNC_MARK, PLACE_CAP_MARK, "=== incentive_mm"])
    # resting orders vs the cap, and the read that must see them all (757614b)
    cap = int(float(env.get("IMM_MAX_TOTAL_RESTING") or 8000))
    m = re.search(r"mkts quoted, (\d+) resting", got.get(CYCLE_MARK, ""))
    if m:
        rest = int(m.group(1))
        room = cap - rest
        st = (FAIL if auto else WARN) if room < RESTING_ROOM_FAIL else WARN if room < RESTING_ROOM_WARN else PASS
        out.append((st, "resting-order room", f"{rest:,} resting of the {cap:,} cap ({room:,} left; at the cap the "
                    f"bot defers EVERY new placement, wave or not; a launcher change + restart, Jack's word)"))
    else:
        out.append((WARN, "resting-order room", "no cycle line with a resting count"))
    start_ln = got.get("=== incentive_mm", "")
    tr = got.get(TRUNC_MARK, "")
    if tr and (_ts_of(tr) or 0) >= (_ts_of(start_ln) or 0):
        out.append((FAIL, "resting-order read", "the bot's own-orders read ran out of pages since its start "
                    f"({tr[21:150].strip()}): it can't see all its orders and re-places them (11/14 doubled "
                    "3,462 sides)"))
    pc = got.get(PLACE_CAP_MARK, "")
    if pc and nts - (_ts_of(pc) or 0) < 600:
        md = re.search(r"(\d+) deferred to next cycle(.*)$", pc)
        if md:
            out.append((INFO, "placement queue", f"{int(md.group(1)):,} placements deferred last cycle "
                        f"{md.group(2).strip()[:120]}: a new wave's orders go out over several cycles "
                        f"(cap {env.get('IMM_MAX_PLACEMENTS_PER_CYCLE', '1000')}/cycle, renewals included)"))
    uv = got.get(UNIVERSE_MARK, "")
    mu = re.search(r"selected across (\d+)/(\d+) events.*?total ~\$([\d,]+) ladder collateral, \$([\d,]+) inventory "
                   r"reserve\); skips (\{.*\})", uv)
    if mu:
        n_ev, max_ev = int(mu.group(1)), int(mu.group(2))
        coll = float(mu.group(3).replace(",", "")) + float(mu.group(4).replace(",", ""))
        budget = float(env.get("IMM_COLLATERAL_BUDGET") or 0)
        nb = re.search(r"'budget': (\d+)", mu.group(5))
        refused = int(nb.group(1)) if nb else 0
        st = WARN if refused or (budget and coll > 0.9 * budget) else PASS
        out.append((st, "collateral budget", f"~${coll:,.0f} modelled (a standard ladder per SELECTED market, any "
                    f"list size; selected markets stay) of ${budget:,.0f}"
                    + (f"; {refused} market(s) refused for budget" if refused else "; none refused")))
        out.append((WARN if n_ev >= 0.95 * max_ev else PASS, "event ceiling",
                    f"{n_ev} of {max_ev} events selected (IMM_MAX_MARKETS)"))
    # restarts: each one re-queues the book; reads within ~15 min of one run low
    try:
        logs = sorted(glob.glob(os.path.join(INC, "incentive-mm-20*.log")), key=os.path.getmtime)[-1:]
        starts = []
        for p in logs:
            for ln in _tail_lines(p, 6_000_000):
                if "=== incentive_mm" in ln:
                    t0 = _ts_of(ln)
                    if t0 and nts - t0 <= RESTART_WINDOW_MIN * 60:
                        starts.append(t0)
        if starts:
            last = max(starts)
            ok_at = datetime.fromtimestamp(last + READ_LOW_MIN * 60, timezone.utc)
            out.append((WARN if nts - last < READ_LOW_MIN * 60 else INFO, "recent restarts",
                        f"{len(starts)} in the last {RESTART_WINDOW_MIN:.0f} min, latest "
                        f"{datetime.fromtimestamp(last, timezone.utc):%H:%MZ}: pacing reads run low until "
                        f"~{ok_at:%H:%MZ}; judge sizes after that"))
    except (OSError, IndexError):
        pass
    # each wave: scan-budget room, and cash on its shards
    try:
        cash = {}
        sys.path.insert(0, HERE)
        from kalshi_reads import kalshi_get
        for row in (kalshi_get("/portfolio/balance") or {}).get("balance_breakdown") or []:
            try:
                cash[str(row.get("exchange_index"))] = float(row.get("balance"))
            except (TypeError, ValueError):
                pass
    except Exception as ex:  # noqa: BLE001
        cash = None
        out.append((WARN, "shard cash", f"balance unreadable ({str(ex)[:80]})"))
    for name, w in waves:
        r = scan_budget_room(w, now=now)
        st = PASS if r["room"] >= 5000 else WARN
        out.append((st, f"{name} scan budget", f"${max(r['room'], 0):,.0f} of listing room: its own "
                    f"${r['live_own']:,.0f} of its ${r['budget']:,.0f}; every wave's ${r['live_all']:,.0f} of "
                    f"${GLOBAL_BUDGET + r['budget']:,.0f} shared (the larger room applies)"))
        if cash is None:
            continue
        sample = _sample_tickers(all_series(w), detect)
        shards = _shards_of(sample) if sample else {}
        for sh, ss in sorted(shards.items()):
            if sh == "0":
                continue                    # shard 0: guard_checks' own check
            c = cash.get(sh)
            if c is None:
                out.append((FAIL if auto else WARN, f"{name} shard-{sh} cash",
                            f"no shard-{sh} balance ({', '.join(sorted(set(ss)))} trade there): orders there get "
                            f"404 insufficient_shard_balance; only Jack moves money"))
                continue
            st = (FAIL if auto else WARN) if c < MIN_CASH_OTHER else PASS
            out.append((st, f"{name} shard-{sh} cash", f"${c:,.0f} free on shard {sh} "
                        f"({', '.join(sorted(set(ss)))} trade there; fills draw on it, shared with any bot on "
                        f"that shard; floor ${MIN_CASH_OTHER:,.0f}; only Jack moves money)"))
        if not shards and sample:
            out.append((WARN, f"{name} shards", "no shard read for its markets"))
    return out


def tier_mult(spec: str, cost: float) -> float:
    """The multiplier a --cheap-tiers spec gives a side tying up `cost` c a contract (first fit)."""
    for part in (spec or "").split(","):
        try:
            c, x = part.split(":")
            if cost <= float(c):
                return float(x)
        except ValueError:
            continue
    return 1.0


def _vals(tune: list, flag: str) -> list:
    return [v for f, v in args_table(tune) if f == flag]


def wave_checks(name: str, w: dict, auto: bool = False) -> list:
    """A registry wave's tune args against the pick-off rules."""
    out = []
    base_tiers = _vals(PRESETS["default"], "--cheap-tiers")[0]
    for group, tune in (("main", w.get("tune") or []), ("near", w.get("near_tune") or [])):
        if group == "near" and not w.get("near"):
            continue
        if group == "main" and not w.get("series"):
            continue
        tag = f"{name} {group}"
        for fl in AUTO_FORBIDDEN:
            if fl in tune:
                out.append((FAIL if auto else WARN, f"{tag} {fl}",
                            f"{fl} {' '.join(_vals(tune, fl))} -- Jack's word only "
                            + ("(no_halts removes the sweep breaker + toxic halts)" if fl == "--no-halts" else "")))
        for spec in _vals(tune, "--cheap-tiers"):
            over = [c for c in range(1, 100) if tier_mult(spec, c) > tier_mult(base_tiers, c) + 1e-9]
            if over:
                out.append((FAIL if auto else WARN, f"{tag} cheap tiers",
                            f"{spec} is above the default preset ({base_tiers}) at {over[0]}-{over[-1]}c"))
        if "--reanchor" in tune or "--follow-book" in tune:
            step = float((_vals(tune, "--reanchor-step") or ["5"])[-1])
            jump = float((_vals(tune, "--reanchor-jump") or ["10"])[-1])
            if step <= 0 or jump <= 0:
                out.append((FAIL, f"{tag} re-anchor cap", f"--reanchor-step {step:g} / --reanchor-jump {jump:g}: "
                            "0 = the uncapped re-anchor the 11/07 House baits fed on"))
            else:
                out.append((PASS, f"{tag} re-anchor cap", f"toward the taker <= {step:g}c a run, held a run "
                            f"after a {jump:g}c+ touch jump"))
        # 90c+ SIDES: no preset cap since 11/08 (Jack: "dont preset that"); a cap there
        # may rest 0 or a real size, never a probe-sized lot -- a 1-lot was the probe the
        # 11/07 bait sweeps took (Jack: "why even have a probe?"; 75 of 90 small wave fills)
        caps = []
        for v in _vals(tune, "--side-cap"):
            if v.startswith(("ALL:", "*:")) and v.count(":") == 2:
                try:
                    caps.append((float(v.split(":")[1]), int(v.split(":")[2])))
                except ValueError:
                    pass
        eff = {x: next((n for c, n in caps if c <= x), None) for x in range(90, 100)}
        probe = sorted({n for n in eff.values() if n is not None and 1 <= n < PROBE_LOT})
        if probe:
            out.append((FAIL if auto else WARN, f"{tag} 90c+ side cap",
                        f"a side tying up 90c+ rests {probe[0]} -- a probe-sized lot, which the 11/07 "
                        f"bait sweeps took; cap at 0 or a real size, or no cap"))
        elif all(n is None for n in eff.values()):
            out.append((PASS, f"{tag} 90c+ side cap", "none: sides tying up 90c+ quote like any other "
                        "(no preset cap, Jack 11/08)"))
        elif all(n == 0 for n in eff.values()):
            out.append((PASS, f"{tag} 90c+ side cap", "sides tying up 90c+ rest nothing (off)"))
        else:
            out.append((PASS, f"{tag} 90c+ side cap", "sides tying up 90c+ rest "
                        + ", ".join(sorted({str(n) for n in eff.values() if n is not None}))))
        out.append((PASS if "--twin-block" in tune else WARN, f"{tag} twin block",
                    "on" if "--twin-block" in tune else "missing: a D/R pair can double the same bet"))
    return out


def scope_checks(series: list, near: list, detect, auto: bool = False) -> list:
    """Each series against the wave rules: verdict, 5+ events, company KPIs, blocklist."""
    out = []
    rows = {r["series"]: r for r in (detect or {}).get("series", [])}
    try:
        sys.path.insert(0, HERE)
        import incentive_mm as imm                  # noqa: E402  (reads only)
    except Exception as ex:  # noqa: BLE001
        imm = None
        out.append((WARN, "scope", f"incentive_mm not importable ({ex}); KPI / blocklist checks skipped"))
    for s in list(series) + [x for x in near if x not in series]:
        r = rows.get(s)
        if imm is not None:
            if getattr(imm, "company_kpi_series", lambda _s: False)(s):
                out.append((FAIL, f"scope {s}", "a company-KPI series (frozen in the bot since 0853106)"))
                continue
            if imm.series_blocklisted(s):
                out.append((FAIL, f"scope {s}", "blocklisted in the bot"))
                continue
        if r is None:
            out.append((FAIL if auto else WARN, f"scope {s}",
                        "not in the detector output" + (" (auto mode lists detector series only)" if auto else "")))
            continue
        want = "NEAR" if s in near and s not in series else "FIT"
        issues = []
        if r["verdict"] not in ("FIT", "NEAR"):
            issues.append((FAIL if auto else WARN, f"scope {s}", f"verdict {r['verdict']} "
                           f"({'; '.join(r.get('why') or []) or r.get('category', '')}) -- Jack's call"))
        elif r["verdict"] != want:
            issues.append((WARN, f"scope {s}", f"verdict {r['verdict']} but listed as {want}"))
        if int(r.get("events") or 0) < MIN_EVENTS:
            issues.append((FAIL if auto else WARN, f"scope {s}", f"{r.get('events')} event(s) lit: a wave needs "
                           f"{MIN_EVENTS}+ in one series (Jack 11/08)"))
        out += issues or [(PASS, f"scope {s}", f"{r['verdict']}, {r['events']} events, {r['markets']} markets, "
                           f"${float(r.get('pool_day') or 0):,.0f}/day")]
    return out


def legacy_checks() -> list:
    """The relight task's hand-written passes (2028 / political / 11/04) -- relit
    markets come back with these args."""
    out = []
    try:
        with open(TASK_CMD, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except OSError:
        return out
    for m in re.finditer(r"echo ===== %date% %time% (pass \d+: [^>]*?) >>[^\n]*\n(?:rem[^\n]*\n)*([^\n]*pvd_wave_tune\.py[^\n]*)", txt):
        label, line = m.group(1).strip(), m.group(2)
        bits = []
        if "--no-halts" in line:
            bits.append("--no-halts")
        sm = re.search(r"--size-mult (\S+)", line)
        if sm:
            bits.append(f"--size-mult {sm.group(1)}")
        tiers = re.findall(r"--(?:cheap-tiers|series-tier) (\S+)", line)
        top = 0.0
        for t in tiers:
            for part in t.split(","):
                try:
                    top = max(top, float(part.rsplit(":", 1)[1]))
                except (ValueError, IndexError):
                    pass
        if top > 10:
            bits.append(f"tiers up to x{top:g} of the base")
        if bits:
            out.append((INFO, f"legacy {label}", "relit markets return with " + ", ".join(bits)
                        + " (Jack's 11/07 settings; change them in run_pvd_scan_waves.cmd only on his word)"))
    return out


def print_checks(checks: list) -> int:
    n_fail = sum(1 for c in checks if c[0] == FAIL)
    for st, name, detail in checks:
        print(f"{st:4}  {name:34} {detail}")
    print(f"preflight: {'FAIL' if n_fail else 'PASS'} ({n_fail} fail, "
          f"{sum(1 for c in checks if c[0] == WARN)} warn)")
    return n_fail


def cmd_preflight(a) -> int:
    reg = load()
    det = None
    if a.detect:
        with open(a.detect, encoding="utf-8") as fh:
            det = json.load(fh)
    checks = guard_checks()
    names = [a.name] if a.name and a.name != "all" else [n for n, w in reg["waves"].items()
                                                          if w.get("enabled", True)] if a.name == "all" else []
    cap_waves = []
    for n in names:
        w = reg["waves"].get(n)
        if not w:
            raise SystemExit(f"no wave {n}")
        checks += wave_checks(n, w, a.auto)
        cap_waves.append((n, w))
        if det is not None:
            checks += scope_checks(w.get("series") or [], w.get("near") or [], det, a.auto)
    if a.series or a.near:
        ser = [s.strip() for s in (a.series or "").split(",") if s.strip()]
        nr = [s.strip() for s in (a.near or "").split(",") if s.strip()]
        checks += scope_checks(ser, nr, det, a.auto)
        budget = a.budget if a.budget is not None else (AUTO_BUDGET_MAX if a.auto else 100000.0)
        cap_waves.append(("new wave", {"series": ser, "near": nr, "budget": budget}))
    if not a.no_capacity:
        checks += capacity_checks(cap_waves, a.auto, detect=det)
    if a.legacy:
        checks += legacy_checks()
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump([{"status": s, "check": n, "detail": d} for s, n, d in checks], fh, indent=1)
    return 1 if print_checks(checks) else 0


# ---------------------------------------------------------------------------
# PICK-OFFS: adding fills on wave markets marked to the real book (ours out).
# Method from the 11/07 review (memory imm-pickoffs-1107): a pick-off = a fill
# that OPENED or ADDED to a position and is now 5c+ against us; the bot's own
# toxic rule (5c+ within 5 min) flagged only 17 of that day's 53, because the
# jumped books stood longer.
# ---------------------------------------------------------------------------
def orderbook_exact_levels(ob: dict):
    fp = (ob or {}).get("orderbook_fp") or {}
    yes = [[round(float(p) * 100, 2), float(q)] for p, q in (fp.get("yes_dollars") or [])]
    no = [[round(float(p) * 100, 2), float(q)] for p, q in (fp.get("no_dollars") or [])]
    return yes, no


def external_touch(yes, no, own) -> tuple:
    """(best external bid, best external ask) in YES cents, our resting orders taken out."""
    own_yes, own_no = {}, {}
    for bs, p, rem in own:
        if bs == "bid":
            own_yes[round(p, 2)] = own_yes.get(round(p, 2), 0.0) + rem
        else:
            k = round(100 - p, 2)
            own_no[k] = own_no.get(k, 0.0) + rem
    bids = [p for p, q in yes if q - own_yes.get(round(p, 2), 0.0) > 1e-9]
    asks = [round(100 - p, 2) for p, q in no if q - own_no.get(round(p, 2), 0.0) > 1e-9]
    return (max(bids) if bids else None), (min(asks) if asks else None)


def _order_side(o: dict) -> str:
    side, act = o.get("side"), o.get("action")
    return "bid" if (side == "yes" and act == "buy") or (side == "no" and act == "sell") else "ask"


def _order_px(o: dict):
    for k in ("yes_price_dollars", "price_dollars"):
        if o.get(k) is not None:
            try:
                px = round(float(o[k]) * 100, 2)
                return px if o.get("side") == "yes" or k == "yes_price_dollars" else round(100 - px, 2)
            except (TypeError, ValueError):
                pass
    return None


def legacy_series() -> dict:
    """{series: legacy wave label} from the relight task's W28 / POL sets."""
    out = {}
    try:
        with open(TASK_CMD, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except OSError:
        return out
    for var, label in (("W28", "2028 election wave (pass 4)"), ("POL", "political wave (passes 1/5)")):
        m = re.search(rf"^set {var}=(.*)$", txt, re.M)
        for s in (m.group(1).split(",") if m else []):
            if s.strip():
                out[s.strip()] = label
    return out


def wave_of(t: str, reg: dict, L: dict, legacy: dict):
    """(wave name, registry?) for a ticker on a wave, else (None, False)."""
    s = t.split("-")[0]
    for name, w in reg["waves"].items():
        if s in all_series(w):
            return name, True
    e = L.get(t)
    if e is None:
        return None, False
    m = re.search(r"\(wave ([^)\s]+)", json.dumps(e))
    if m and m.group(1) in reg["waves"]:
        return m.group(1), True
    return legacy.get(s, "11/04 election wave + other listed (pass 6)"), False


def fills_since(t0: float) -> list:
    out = []
    d = datetime.fromtimestamp(t0, timezone.utc).date()
    end = datetime.now(timezone.utc).date()
    while d <= end:
        p = os.path.join(INC, f"fills_{d.isoformat()}.jsonl")
        try:
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if float(r.get("ts") or 0) >= t0:
                        out.append(r)
        except OSError:
            pass
        d += timedelta(days=1)
    return out


def adding_count(r: dict) -> float:
    pb, pa = float(r.get("pos_before") or 0), float(r.get("pos_after") or 0)
    if pb * pa < 0:
        return abs(pa)
    return max(0.0, abs(pa) - abs(pb))


def pickoff_scan(a, reg: dict) -> dict:
    now = datetime.now(timezone.utc)
    t0 = now.timestamp() - a.hours * 3600.0
    try:
        with open(LIST, encoding="utf-8") as fh:
            L = json.load(fh).get("markets") or {}
    except (OSError, ValueError):
        L = {}
    legacy = legacy_series()
    agg: dict = {}
    n_fills = 0
    for r in fills_since(t0):
        t = r.get("ticker") or ""
        wave, in_reg = wave_of(t, reg, L, legacy)
        if wave is None or (a.name not in (None, "all") and wave != a.name):
            continue
        n_fills += 1
        add = adding_count(r)
        if add < 1:
            continue
        side = r.get("our_book_side") or ("bid" if float(r.get("pos_after") or 0) > float(r.get("pos_before") or 0) else "ask")
        k = (t, side)
        g = agg.setdefault(k, {"ticker": t, "series": t.split("-")[0], "wave": wave, "registry": in_reg,
                               "side": side, "contracts": 0.0, "cost_c": 0.0, "fills": 0, "first": None,
                               "last": None, "fill_ids": []})
        px = float(r.get("yes_price_cents") or r.get("our_price_cents") or 0)
        g["contracts"] += add
        g["cost_c"] += add * px
        g["fills"] += 1
        ts_ = datetime.fromtimestamp(float(r["ts"]), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        g["first"] = g["first"] or ts_
        g["last"] = ts_
        g["fill_ids"].append(r.get("fill_id"))
    own: dict = {}
    if agg:
        try:
            sys.path.insert(0, HERE)
            from kalshi_reads import kalshi_get_all
            for o in kalshi_get_all("/portfolio/orders", {"status": "resting", "limit": 1000}, items_key="orders"):
                px = _order_px(o)
                if px is None:
                    continue
                rem = float(o.get("remaining_count_fp") or o.get("remaining_count") or 0)
                own.setdefault(o.get("ticker"), []).append((_order_side(o), px, rem))
        except Exception as ex:  # noqa: BLE001
            print(f"! resting orders unreadable ({ex}); books marked WITH our orders", file=sys.stderr)
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            pos_now = (json.load(fh) or {}).get("own_pos") or {}
    except (OSError, ValueError):
        pos_now = {}
    rows = []
    if agg:
        sys.path.insert(0, HERE)
        from kalshi_reads import read
    for (t, side), g in sorted(agg.items()):
        vw = g["cost_c"] / g["contracts"]
        eb = ea = None
        try:
            yes, no = orderbook_exact_levels(read(f"/markets/{t}/orderbook"))
            eb, ea = external_touch(yes, no, own.get(t, []))
        except Exception as ex:  # noqa: BLE001
            g["mark_error"] = str(ex)[:80]
        mark = (eb + ea) / 2 if eb is not None and ea is not None else (eb if side == "bid" else ea)
        g.update({"vwap_c": round(vw, 2), "ext_bid": eb, "ext_ask": ea, "mark_c": mark,
                  "pos_now": float(pos_now.get(t, 0.0))})
        if mark is None:
            g.update({"adverse_c": None, "adverse_usd": None, "flag": False})
        else:
            adv = (vw - mark) if side == "bid" else (mark - vw)
            usd = adv * g["contracts"] / 100.0
            g.update({"adverse_c": round(adv, 2), "adverse_usd": round(usd, 2),
                      "flag": adv >= a.min_c and usd >= a.min_usd})
        rows.append(g)
    flagged = [g for g in rows if g["flag"]]
    by_series: dict = {}
    for g in flagged:
        by_series.setdefault((g["wave"], g["series"]), []).append(g["ticker"])
    trips = [{"wave": w_, "series": s_, "markets": sorted(set(ts_))} for (w_, s_), ts_ in by_series.items()
             if len(set(ts_)) >= a.series_trip]
    return {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "hours": a.hours, "wave_fills": n_fills,
            "rows": rows, "flagged": flagged, "series_trips": trips,
            "adverse_usd": round(sum(g["adverse_usd"] or 0 for g in rows if (g["adverse_usd"] or 0) > 0), 2),
            "flagged_usd": round(sum(g["adverse_usd"] for g in flagged), 2),
            "params": {"min_c": a.min_c, "min_usd": a.min_usd, "series_trip": a.series_trip}}


def _base_size(w: dict, series: str) -> int:
    if series in (w.get("near") or []) and series not in (w.get("series") or []):
        return int((w.get("near_scan") or {}).get("live", 80))
    return int(w.get("scan_size") or 200)


def cmd_pickoffs(a) -> int:
    reg = load()
    res = pickoff_scan(a, reg)
    try:
        with open(PICK_STATE, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    alerted = st.get("alerted") or {}
    acted = st.get("acted") or {}
    new = [g for g in res["flagged"] if f"{g['ticker']}|{g['side']}|{g['fill_ids'][0]}" not in alerted]
    res["new"] = new
    res["acted"] = []
    if a.act:
        touched, pending = set(), {}
        for g in new + res["flagged"]:
            if not g["registry"] or g["ticker"] in acted:
                continue
            w = reg["waves"][g["wave"]]
            if not w.get("enabled", True):
                continue
            key = "near_tune" if g["series"] in (w.get("near") or []) and g["series"] not in (w.get("series") or []) else "tune"
            tune = w.setdefault(key, list(PRESETS["near"]) if key == "near_tune" else [])
            if any(v.startswith(g["ticker"] + ":") for v in _vals(tune, "--pin")):
                continue
            n = _base_size(w, g["series"])
            tune += ["--pin", f"{g['ticker']}:{n}"]
            acted[g["ticker"]] = {"at": res["at"], "wave": g["wave"], "pin": n, "adverse_usd": g["adverse_usd"]}
            res["acted"].append({"ticker": g["ticker"], "wave": g["wave"], "pin": n, "why": "pick-off"})
            touched.add((g["wave"], key))
            pending.setdefault((g["wave"], key), []).append(("--pin", f"{g['ticker']}:{n}", g["ticker"]))
        for tr in res["series_trips"]:
            w = reg["waves"].get(tr["wave"])
            k_ = "S:" + tr["series"]
            if w is None or k_ in acted or not w.get("enabled", True):
                continue
            key = "near_tune" if tr["series"] in (w.get("near") or []) and tr["series"] not in (w.get("series") or []) else "tune"
            tune = w.setdefault(key, list(PRESETS["near"]) if key == "near_tune" else [])
            if any(v.startswith(tr["series"] + ":") for v in _vals(tune, "--pin-series")):
                continue
            n = _base_size(w, tr["series"])
            tune += ["--pin-series", f"{tr['series']}:{n}"]
            acted[k_] = {"at": res["at"], "wave": tr["wave"], "pin": n, "markets": tr["markets"]}
            res["acted"].append({"series": tr["series"], "wave": tr["wave"], "pin": n, "why": "series trip"})
            touched.add((tr["wave"], key))
            pending.setdefault((tr["wave"], key), []).append(("--pin-series", f"{tr['series']}:{n}", k_))
        # each wave's pins go onto a FRESH registry read under its pass lock (a setarg made
        # since this command loaded the registry is kept), then that wave re-tunes now
        for (wname, key), adds in sorted(pending.items()):
            try:
                with wave_pass_lock(wname, 600, "pickoffs --act"):
                    reg2 = load()
                    w = reg2["waves"].get(wname)
                    if w is None:
                        continue
                    tune = w.setdefault(key, list(PRESETS["near"]) if key == "near_tune" else [])
                    for flag, val, _k in adds:
                        if not any(v.startswith(val.split(":")[0] + ":") for v in _vals(tune, flag)):
                            tune += [flag, val]
                    save(reg2)
                    print(f"== {wname}: pinned at base, re-tuning now")
                    run_one(tune_cmd(wname, w, True, "near" if key == "near_tune" else "main"), tail=1)
            except PassLockBusy:
                print(f"== {wname}: a pass holds its lock; the pins wait for the next pick-off run")
                for _f, _v, k in adds:
                    acted.pop(k, None)
                res["acted"] = [x for x in res["acted"]
                                if (x.get("ticker") or "S:" + str(x.get("series"))) not in {k for _f, _v, k in adds}]
    print(f"pickoffs {res['at']} (last {a.hours:g} h): {res['wave_fills']} fills on wave markets, "
          f"{len(res['rows'])} market-sides added to, {len(res['flagged'])} flagged "
          f"(>= {a.min_c:g}c and ${a.min_usd:g} against us, our orders out of the book), "
          f"${res['flagged_usd']:,.0f} flagged; {len(new)} new; series trips: "
          + (", ".join(f"{t_['series']} ({len(t_['markets'])})" for t_ in res["series_trips"]) or "none"))
    for g in sorted(res["rows"], key=lambda x: (x["adverse_usd"] or 0), reverse=True)[:a.top]:
        if g["adverse_c"] is None:
            mk = "no external book"
        else:
            mk = (f"mark {g['mark_c']:.1f} ({g['ext_bid']}x{g['ext_ask']}) "
                  + (f"{g['adverse_c']:.1f}c against us, -${g['adverse_usd']:,.0f}" if g["adverse_c"] > 0
                     else f"{-g['adverse_c']:.1f}c our way, +${-g['adverse_usd']:,.0f}"))
        print(f"  {'PICKOFF' if g['flag'] else '       '} {g['ticker']:38} {g['side']:3} "
              f"{g['contracts']:>7,.0f} @ {g['vwap_c']:5.1f}  {mk}  held {g['pos_now']:+,.0f}  "
              f"{g['wave'][:28]}{'' if g['registry'] else ' [legacy]'}  {g['last'][11:16]}Z")
    for x in res["acted"]:
        print(f"  ACTED {x.get('ticker') or 'series ' + x['series']}: pinned at {x['pin']} a side in {x['wave']} ({x['why']})")
    legacy_flagged = [g for g in res["flagged"] if not g["registry"]]
    if legacy_flagged:
        print(f"  {len(legacy_flagged)} flagged market(s) on legacy waves: stand them down by hand "
              f"(a --pin in run_pvd_scan_waves.cmd, Jack's word) -- " + ", ".join(g["ticker"] for g in legacy_flagged[:6]))
    if a.update_state:
        for g in res["flagged"]:
            alerted.setdefault(f"{g['ticker']}|{g['side']}|{g['fill_ids'][0]}", res["at"])
        cut = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        st = {"alerted": {k: v for k, v in alerted.items() if v >= cut}, "acted": acted, "last": res["at"]}
        os.makedirs(os.path.dirname(PICK_STATE), exist_ok=True)
        with open(PICK_STATE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=1)
        os.replace(PICK_STATE + ".tmp", PICK_STATE)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
    return 3 if (new or res["acted"]) else 0


# ---------------------------------------------------------------------------
# DARK CHECK (module docstring): listed vs resting on Kalshi, with the reason
DARK_STATE = os.environ.get("PVD_DARK_STATE") or os.path.join(HERE, "run-logs", "pvd-scan", "darkcheck_state.json")
DARK_REALERT_H = 24.0       # the same market + reason alerts again after this long
# ALERT ONLY ON WHAT IS REAL AND STAYS (Jack 2026-11-11: "stop false alarming", after two
# false DARK texts: 17 settled markets at 14:17Z, a breaker's 10-min hold at 15:31Z).
# A dark market counts only when the previous check (15-45 min earlier) saw it dark too,
# and then only:
#   * under the OLD-WAVE LOCK both times (the 11/11 failure, which --act tries to fix
#     the first time: still dark = the fix did not take), or
#   * WAVE-WIDE: DARK_SYS_MIN+ markets AND DARK_SYS_FRAC of those checked dark both
#     times, whatever the reason (11/11: 723 of 724).
# Nothing else alerts (a breaker or tape pause, a restart, one event, a dead book, a
# settled market, the payout floor): it stays in darkcheck_last.json.
DARK_SYS_MIN = 100
DARK_SYS_FRAC = 0.25
DARK_PERSIST_GAP_MIN = (10.0, 45.0)   # the previous check counts if it ran this long ago
DARK_HOLD_GRACE_MIN = 5.0   # a hold that ended this recently still explains a dark market (re-placing)
# the bot's breaker / halt holds in imm_state.json: {key: until epoch, or {"hold_until": ..}};
# a key is an event, a ticker, or "ticker|side"
HOLD_KEYS = ("sweep_until", "sweep_hold", "sweep_esc", "wave_hold", "grp_hold", "breaker_until",
             "toxic_halt_until", "toxic_event_halt_until", "event_live_halt", "event_depth_halt")


def active_holds(state: dict, nts: float) -> dict:
    """{key: (hold name, until)} for every breaker / halt hold in the bot's state that
    is running or ended within DARK_HOLD_GRACE_MIN."""
    out: dict = {}
    for name in HOLD_KEYS:
        for k, v in (state.get(name) or {}).items():
            until = v.get("hold_until", v.get("until")) if isinstance(v, dict) else v
            try:
                until = float(until)
            except (TypeError, ValueError):
                until = float("inf") if v else 0.0       # a latch with no time: running
            if until >= nts - DARK_HOLD_GRACE_MIN * 60:
                prev = out.get(str(k))
                if prev is None or until > prev[1]:
                    out[str(k)] = (name, until)
    return out


def hold_for(t: str, holds: dict):
    """The hold covering market `t` (its ticker, its event, or "ticker|side"), else None."""
    ev = t.rsplit("-", 1)[0]
    for k in (t, ev, f"{t}|bid", f"{t}|ask"):
        if k in holds:
            return holds[k]
    return None
DARK_BOT_SETTLE_MIN = 10.0  # right after a bot restart orders are being re-placed: no verdict
DARK_CYCLE_STALE_MIN = 10.0  # no cycle line this long: the bot is down (its health alert's job)
LOCK_REASON = "old-wave lock"
SELECTION_WHY = {
    "selected": "selected, resting nothing (a guard: lone-ref, pvd guard, twin check, caps or cash)",
    "cutoff": "the bot's cutoff (no verified date, or past it)",
    "cutoff_passed": "the bot's cutoff passed",
    "payout_floor": "under the payout floor (projects under $1 this period)",
    "hopeless": "hopeless (our share too small to clear the bar)",
    "admit_pending": "admit pending (the 10-min wait)",
    "zero_yield": "zero yield (no side it can earn on)",
    "one_sided": "one-sided book",
    "extreme_mid": "mid outside the fresh-entry band",
    "event_top_n": "event top-N (other markets of its event quote)",
    "manual": "manual standoff (account orders or positions on the event)",
}


def _ts_of(s) -> "float | None":
    """Epoch seconds of the first ISO time (minutes or seconds; "T" or the bot
    log's space between date and time) in `s`, else None."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?", str(s or ""))
    if not m:
        return None
    d, hh, mm, ss = m.group(1), m.group(2), m.group(3), m.group(4) or "00"
    return datetime.strptime(f"{d}T{hh}:{mm}:{ss}", "%Y-%m-%dT%H:%M:%S").replace(
        tzinfo=timezone.utc).timestamp()


def dark_off_reason(e: dict, wave: "str | None", reg: dict) -> "str | None":
    """Why a listed market rests nothing ON PURPOSE (not a dark alert), else None."""
    tp = e.get("tpause") if isinstance(e.get("tpause"), dict) else None
    if tp and tp.get("mode") == "off":
        return f"paused on purpose (--pause-at {e.get('tpause', {}).get('pc', '?')}c)"
    try:
        if int(e.get("price_min", 1)) > int(e.get("price_max", 99)):
            return "paused on purpose (band)"
    except (TypeError, ValueError):
        pass
    if e.get("bid_size") == 0 and e.get("ask_size") == 0:
        return "both sides sized 0 (--side-cap)"
    w = (reg.get("waves") or {}).get(wave or "")
    if w is not None and not w.get("enabled", True):
        return f"its wave {wave} is off"
    return None


def latest_selection(tickers, now: datetime) -> dict:
    """{ticker: latest selection decision} from the bot's selection_events sinks
    (today's, then yesterday's for the rest)."""
    want, out = set(tickers), {}
    for d in (now.date(), (now - timedelta(days=1)).date()):
        p = os.path.join(INC, f"selection_events_{d.isoformat()}.jsonl")
        got: dict = {}
        try:
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    i = ln.find('"ticker": "')
                    if i < 0:
                        continue
                    t = ln[i + 11: ln.find('"', i + 11)]
                    if t in want and t not in out:
                        try:
                            got[t] = json.loads(ln).get("decision")
                        except ValueError:
                            pass
        except OSError:
            continue
        out.update(got)
        if len(out) >= len(want):
            break
    return out


def market_statuses(tickers) -> dict:
    """{ticker: Kalshi market status} for `tickers` (signed /markets reads, 50 a
    call); a failed read leaves those out (judged as before)."""
    out: dict = {}
    tickers = list(tickers)
    if not tickers:
        return out
    try:
        sys.path.insert(0, HERE)
        from kalshi_reads import kalshi_get_all
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            for m in kalshi_get_all("/markets", {"tickers": ",".join(chunk), "limit": len(chunk)},
                                    items_key="markets", max_pages=2):
                if m.get("ticker"):
                    out[m["ticker"]] = str(m.get("status") or "").lower()
    except Exception as ex:  # noqa: BLE001
        print(f"! market status read failed ({ex}); dark markets judged without it", file=sys.stderr)
    return out


GUARD_WHY = {
    "cannot_qualify": "the bot's depth gate: a side has no book to qualify on (a decided / dead book)",
    "cutoff_passed": "the bot's cutoff passed",
    "band_both_out": "both sides outside the bot's price band",
    "release_pause": "a release pause",
    "league_live": "a live game",
    "manual_yield": "a manual standoff",
}


def latest_guard(tickers, now: datetime) -> dict:
    """{ticker: the guard standing it aside now, or None once cleared} from the
    bot's guard_skips sinks (today's, then yesterday's for the rest)."""
    want, out = set(tickers), {}
    for d in (now.date(), (now - timedelta(days=1)).date()):
        p = os.path.join(INC, f"guard_skips_{d.isoformat()}.jsonl")
        got: dict = {}
        try:
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    i = ln.find('"ticker": "')
                    if i < 0:
                        continue
                    t = ln[i + 11: ln.find('"', i + 11)]
                    if t in want and t not in out:
                        try:
                            r = json.loads(ln)
                        except ValueError:
                            continue
                        if r.get("kind") == "clear":
                            got[t] = None
                        elif r.get("kind") in ("enter", "snapshot") and r.get("guard"):
                            got[t] = r["guard"]
        except OSError:
            continue
        out.update(got)
        if len(out) >= len(want):
            break
    return out


def bot_status(now: datetime) -> dict:
    """The bot's latest start and latest cycle line from the newest day log's tail."""
    logs = sorted(glob.glob(os.path.join(INC, "incentive-mm-20*.log")), key=os.path.getmtime)
    st = {"log": logs[-1] if logs else None, "started": None, "cycled": None}
    if not logs:
        return st
    try:
        with open(logs[-1], "rb") as fh:
            fh.seek(max(0, os.path.getsize(logs[-1]) - 4_000_000))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return st
    for ln in tail:
        if "=== incentive_mm" in ln:
            st["started"] = _ts_of(ln)
        elif "mkts quoted" in ln:
            st["cycled"] = _ts_of(ln)
    if st["started"] is None:     # the start is older than the tail: the log's first line
        try:
            with open(logs[-1], encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    if "=== incentive_mm" in ln:
                        st["started"] = _ts_of(ln)
                        break
        except OSError:
            pass
    return st


def dark_scan(a, reg: dict, now: "datetime | None" = None, resting=None, own_pos=None,
              selection=None, bot=None, guards=None, statuses=None, holds=None) -> dict:
    """Every listed wave market (a live entry of the list file) at least
    --min-age minutes into its listing: two-sided / one-sided / dark on Kalshi,
    and each dark one's reason. `resting` {ticker: {side}} / `own_pos` /
    `selection` / `bot` are read live when None (tests pass them)."""
    now = now or datetime.now(timezone.utc)
    nts = now.timestamp()
    res = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "min_age_min": a.min_age, "deferred": None,
           "listed": 0, "checked": 0, "too_new": 0, "two_sided": 0, "one_sided": 0, "dark": 0,
           "by_reason": {}, "dark_rows": [], "one_rows": [], "new": [], "acted": []}
    bot = bot if bot is not None else bot_status(now)
    res["bot"] = {k: (datetime.fromtimestamp(v, timezone.utc).strftime("%H:%M:%SZ")
                      if isinstance(v, float) else v) for k, v in bot.items()}
    if bot.get("started") and nts - bot["started"] < DARK_BOT_SETTLE_MIN * 60:
        res["deferred"] = (f"the bot restarted {(nts - bot['started']) / 60:.0f} min ago; "
                           f"orders are still being re-placed")
        return res
    if not bot.get("cycled") or nts - bot["cycled"] > DARK_CYCLE_STALE_MIN * 60:
        res["deferred"] = "the bot has not logged a cycle in 10+ min (its health alert covers that)"
        return res
    try:
        with open(LIST, encoding="utf-8") as fh:
            L = json.load(fh).get("markets") or {}
    except (OSError, ValueError) as ex:
        res["deferred"] = f"list unreadable ({ex})"
        return res
    sw = os.path.join(INC, "UNWIND_ONLY_WAVES")
    sw_at = os.path.getmtime(sw) if os.path.exists(sw) else None
    res["switch_at"] = (datetime.fromtimestamp(sw_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        if sw_at else None)
    live = {}
    for t, e in L.items():
        # (the cheap-side books live in their own file; an entry's "cheap" key
        # here is the wave tune's cheap-tier note, not that flag)
        stop = _ts_of(e.get("stop"))
        if stop is None or stop <= nts:
            continue
        live[t] = e
    res["listed"] = len(live)
    if resting is None:
        sys.path.insert(0, HERE)
        from kalshi_reads import kalshi_get_all
        resting = {}
        for o in kalshi_get_all("/portfolio/orders", {"status": "resting", "limit": 1000}, items_key="orders"):
            if float(o.get("remaining_count_fp") or o.get("remaining_count") or 0) >= 1:
                resting.setdefault(o.get("ticker"), set()).add(_order_side(o))
    if own_pos is None or holds is None:
        try:
            with open(STATE_FILE, encoding="utf-8") as fh:
                _st = json.load(fh) or {}
        except (OSError, ValueError):
            _st = {}
        if own_pos is None:
            own_pos = _st.get("own_pos") or {}
        if holds is None:
            holds = active_holds(_st, nts)
    legacy = legacy_series()
    aged = {}
    for t, e in live.items():
        # judged from the latest of its period start, its listing and its stamp: a market a
        # tune just stamped (wave_added) gets the bot's reload and placement time too
        since = max([x for x in (_ts_of(e.get("period_start")), _ts_of(e.get("added")),
                                 _ts_of(e.get("wave_added"))) if x] or [0.0])
        if nts - since < a.min_age * 60:
            res["too_new"] += 1
            continue
        aged[t] = (e, since)
    res["checked"] = len(aged)
    dark = [t for t in aged if not resting.get(t)]
    if selection is None:
        selection = latest_selection(dark, now) if dark else {}
    if guards is None:
        guards = latest_guard(dark, now) if dark else {}
    if statuses is None:
        statuses = market_statuses(dark) if dark else {}
    reasons = {}
    for t, (e, since) in sorted(aged.items()):
        sides = resting.get(t) or set()
        wave, in_reg = wave_of(t, reg, L, legacy)
        pos = float(own_pos.get(t, 0.0) or 0.0)
        if len(sides) == 2:
            res["two_sided"] += 1
            continue
        if len(sides) == 1:
            res["one_sided"] += 1
            res["one_rows"].append({"ticker": t, "series": t.split("-")[0], "wave": wave, "side": sorted(sides)[0],
                                    "pos": pos})
            continue
        off = dark_off_reason(e, wave, reg)
        stamped = _ts_of(e.get("wave_added"))
        status = statuses.get(t)
        if off:
            why, kind = off, "off"
        elif status and status not in ("active", "open"):
            # (11/11 14:17Z: 17 settled KXSTATELEG / KXPASEN markets, still on the list
            # until their old program's stop, read as "unexplained" and texted)
            why, kind = f"closed on Kalshi ({status}): nothing to quote", "off"
        elif abs(pos) >= 1:
            why, kind = f"held {pos:+,.0f}: the unwind rests nothing now", "held"
        elif sw_at and not (stamped and stamped > sw_at):
            why, kind = (f"{LOCK_REASON}: listed before the 11/08 UNWIND_ONLY_WAVES switch and not "
                         f"stamped by a tune pass (wave_added)"), "lock"
        else:
            # the bot's own reason: a guard standing it aside now (guard_skips), else its
            # selection decision; "bot" = a deliberate decision of the bot's (a dead
            # book's depth gate, a cutoff, the payout floor ...), "unexplained" = selected
            # with no guard on record, or no record at all
            g, dec = guards.get(t), selection.get(t)
            hold = hold_for(t, holds)
            if hold:
                # a breaker / halt hold on its event (imm_state): the bot pulled it on
                # purpose and re-quotes when the hold ends (11/11 15:21Z: the sweep breaker
                # held KXDPRESPRIMARY-28KY 600 s after ABES's 200 ask filled)
                why = (f"the bot's {hold[0]} hold on its event (a breaker / halt; re-quotes when it ends)")
                kind = "bot"
            elif g:
                why, kind = GUARD_WHY.get(g, f"the bot's {g} guard"), "bot"
            elif dec and dec != "selected":
                why, kind = SELECTION_WHY.get(dec, f"selection: {dec}"), "bot"
            elif dec == "selected":
                why, kind = SELECTION_WHY["selected"], "unexplained"
            else:
                why, kind = "no selection record (not in the bot's universe)", "unexplained"
        reasons[why] = reasons.get(why, 0) + 1
        res["dark_rows"].append({"ticker": t, "series": t.split("-")[0], "wave": wave, "registry": in_reg,
                                 "reason": why, "kind": kind, "pos": pos,
                                 "listed_at": datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
                                 if since else None})
    res["dark"] = len(res["dark_rows"])
    res["by_reason"] = dict(sorted(reasons.items(), key=lambda kv: -kv[1]))
    return res


def dark_act(res: dict, reg: dict, write: bool = True) -> list:
    """--act on the LOCK case: a series of an enabled registry wave is re-tuned
    now (the tune stamps wave_added); any other series is adopted into a
    registry wave on the default preset, after the auto preflight (guards and
    the wave's args; a FAIL adopts nothing). Returns what it did."""
    lock = [r for r in res["dark_rows"] if r["kind"] == "lock"]
    if not lock:
        return []
    acted = []
    by_series: dict = {}
    for r in lock:
        by_series.setdefault(r["series"], []).append(r)
    retune: dict = {}
    adopt: list = []
    for s in sorted(by_series):
        owner = next((n for n, w in reg["waves"].items() if s in all_series(w) and w.get("enabled", True)), None)
        if owner:
            retune.setdefault(owner, []).append(s)
        else:
            adopt.append(s)
    for name, ss in sorted(retune.items()):
        rc = 0
        try:
            with wave_pass_lock(name, 300 if write else 0, "darkcheck --act"):
                w = dict(load()["waves"].get(name) or reg["waves"][name])
                w["series"] = [s for s in w.get("series") or [] if s in ss]
                w["near"] = [s for s in w.get("near") or [] if s in ss]
                for group in ("main", "near"):
                    if (w["near"] if group == "near" else w["series"]):
                        rc |= run_one(tune_cmd(name, w, write, group), tail=1) if write else 0
        except PassLockBusy:
            acted.append({"action": "not retuned", "wave": name, "series": ss,
                          "why": "a pass holds its lock (it stamps them itself)",
                          "markets": sum(len(by_series[s]) for s in ss)})
            continue
        acted.append({"action": "retuned", "wave": name, "series": ss, "rc": rc,
                      "markets": sum(len(by_series[s]) for s in ss)})
    if adopt:
        name = "w" + datetime.now(timezone.utc).strftime("%m%d") + "-adopt"
        since_ts = min(_ts_of(r["listed_at"]) or 0 for s in adopt for r in by_series[s]) - 1800
        since = datetime.fromtimestamp(max(since_ts, 0), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        w = reg["waves"].get(name)
        probe = dict(w) if w else {"series": [], "near": [], "tune": list(PRESETS["default"]),
                                   "near_tune": list(PRESETS["near"])}
        probe["series"] = sorted(set(probe.get("series") or []) | set(adopt))
        n_fail = print_checks(guard_checks() + wave_checks(name, probe, auto=True))
        if n_fail:
            acted.append({"action": "not adopted", "wave": name, "series": adopt,
                          "why": f"preflight: {n_fail} FAIL", "markets": sum(len(by_series[s]) for s in adopt)})
            return acted
        if w is None:
            w = {"series": [], "since": since, "go_live": now_z(), "port": next_port(reg),
                 "dir": f"pvd-watch-{name}", "scan_size": 200, "budget": AUTO_BUDGET_MAX, "stop_cap": None,
                 "preset": "default", "tune": list(PRESETS["default"]), "near": [],
                 "near_tune": list(PRESETS["near"]), "near_scan": {"live": 80, "dead": 120, "decided": 300},
                 "enabled": True, "added": now_z(),
                 "note": "darkcheck --act: listed markets dark under the old-wave lock (Jack 2026-11-11: "
                         "\"if the 2028 previously quoted markets get relit, they should be quoted\")"}
        w["series"] = probe["series"]
        if _ts_of(since) < (_ts_of(w.get("since")) or 0):
            w["since"] = since
        if write:
            try:
                with wave_pass_lock(name, 300, "darkcheck --act adopt"):
                    reg2 = load()                       # keep any registry edit made meanwhile
                    old = reg2["waves"].get(name)
                    if old:
                        old["series"] = sorted(set(old.get("series") or []) | set(w["series"]))
                        if _ts_of(w["since"]) < (_ts_of(old.get("since")) or 0):
                            old["since"] = w["since"]
                        w = old
                    reg2["waves"][name] = w
                    save(reg2)
                    only = dict(w, series=adopt, near=[])
                    rc = run_one(tune_cmd(name, only, True), tail=1)
            except PassLockBusy:
                acted.append({"action": "not adopted", "wave": name, "series": adopt,
                              "why": "a pass holds its lock; next check retries",
                              "markets": sum(len(by_series[s]) for s in adopt)})
                return acted
        else:
            rc = 0
        acted.append({"action": "adopted", "wave": name, "series": adopt, "rc": rc,
                      "markets": sum(len(by_series[s]) for s in adopt)})
    return acted


def cmd_darkcheck(a) -> int:
    reg = load()
    res = dark_scan(a, reg)
    if res["deferred"]:
        print(f"darkcheck {res['at']}: deferred -- {res['deferred']}")
        if a.json:
            with open(a.json, "w", encoding="utf-8") as fh:
                json.dump(res, fh, indent=1)
        return 0
    try:
        with open(DARK_STATE, encoding="utf-8") as fh:
            st = json.load(fh)
    except (OSError, ValueError):
        st = {}
    alerted = st.get("alerted") or {}
    nts = datetime.now(timezone.utc).timestamp()
    # the previous check's dark markets (any reason but on purpose / held), if it ran
    # 10-45 min ago: only darkness seen twice counts
    prev_at = _ts_of(st.get("prev_at"))
    gap = (nts - prev_at) / 60 if prev_at else None
    prev = (st.get("prev") or {}) if gap is not None and \
        DARK_PERSIST_GAP_MIN[0] <= gap <= DARK_PERSIST_GAP_MIN[1] else {}
    live_rows = [r for r in res["dark_rows"] if r["kind"] not in ("off", "held")]
    persist = [r for r in live_rows if r["ticker"] in prev]
    p_lock = [r for r in persist if r["kind"] == "lock" and prev.get(r["ticker"]) == "lock"]
    wave_wide = len(persist) >= max(DARK_SYS_MIN, DARK_SYS_FRAC * max(res["checked"], 1))
    cand = {r["ticker"]: r for r in (persist if wave_wide else p_lock)}
    new = [r for r in cand.values()
           if (alerted.get(r["ticker"]) or {}).get("reason") != r["reason"]
           or nts - (_ts_of((alerted.get(r["ticker"]) or {}).get("at")) or 0) > DARK_REALERT_H * 3600]
    order = {"lock": 0, "unexplained": 1, "bot": 2}
    new.sort(key=lambda r: (order.get(r["kind"], 3), r["ticker"]))
    res["new"] = new
    res["persist"] = len(persist)
    res["wave_wide"] = wave_wide
    if a.act:
        res["acted"] = dark_act(res, reg, write=True)
    alert = bool(new)
    res["alert"] = alert
    n_lock = sum(1 for r in live_rows if r["kind"] == "lock")
    head = (f"darkcheck {res['at']}: {res['listed']} listed, {res['checked']} checked "
            f"({res['too_new']} under {a.min_age:g} min), {res['two_sided']} two-sided, "
            f"{res['one_sided']} one-sided, {res['dark']} dark ({n_lock} under the old-wave lock); "
            f"{len(persist)} dark at the last check too"
            + (f" -- WAVE-WIDE" if wave_wide else "")
            + (f"; ALERT {len(new)}" if alert else "; no alert"))
    print(head)
    for why, n in res["by_reason"].items():
        print(f"   {n:5d}  {why}")
    for x in res["acted"]:
        print(f"   ACT {x['action']} {x['wave']}: {', '.join(x['series'])} ({x['markets']} mkts)"
              + (f" -- {x['why']}" if x.get("why") else f" rc={x.get('rc')}"))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
    if a.update_state:
        for r in new:
            alerted[r["ticker"]] = {"reason": r["reason"], "at": res["at"]}
        live_t = {r["ticker"] for r in res["dark_rows"]}
        st["alerted"] = {t: v for t, v in alerted.items() if t in live_t}
        st["prev"] = {r["ticker"]: r["kind"] for r in live_rows}
        st["prev_at"] = res["at"]
        st["at"] = res["at"]
        os.makedirs(os.path.dirname(DARK_STATE), exist_ok=True)
        with open(DARK_STATE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=1)
        os.replace(DARK_STATE + ".tmp", DARK_STATE)
    return 3 if alert else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("add")
    x.add_argument("name")
    x.add_argument("--series", default="", help="FIT series (the preset); --near for NEAR ones")
    x.add_argument("--since", required=True)
    x.add_argument("--go-live")
    x.add_argument("--port", type=int)
    x.add_argument("--budget", type=float, default=100000.0)
    x.add_argument("--scan-size", type=int, default=200)
    x.add_argument("--stop-cap")
    x.add_argument("--preset", choices=sorted(PRESETS), default="default")
    x.add_argument("--tune-extra", help="more tune args, e.g. \"--pin-series KXFOO:300\"")
    x.add_argument("--near", help="NEAR series (settle in 1-14 d): their own group, dead/live at --near-dead/--near-live")
    x.add_argument("--near-live", type=int, default=80)
    x.add_argument("--near-dead", type=int, default=120)
    x.add_argument("--note")
    x.add_argument("--replace", action="store_true")
    x.add_argument("--auto", action="store_true",
                   help="auto mode: default preset, budget <= 50k, and every preflight gate must pass, or nothing is written")
    x.add_argument("--detect", help="wave_detect.py --json output (required with --auto)")
    x.set_defaults(fn=cmd_add)
    x = sub.add_parser("preflight", help="PASS / WARN / FAIL for every guard a wave relies on (exit 1 on a FAIL)")
    x.add_argument("name", nargs="?", help="a registry wave, or 'all' (enabled ones)")
    x.add_argument("--series", help="series to check before an add")
    x.add_argument("--near", help="NEAR series to check before an add")
    x.add_argument("--detect", help="wave_detect.py --json output (verdicts and event counts)")
    x.add_argument("--auto", action="store_true", help="auto-mode rules: Jack-only settings and scope gaps FAIL")
    x.add_argument("--legacy", action="store_true", help="also list the relight task's hand-written passes")
    x.add_argument("--budget", type=float, help="the new wave's budget for its scan-budget room (default 100k; 50k --auto)")
    x.add_argument("--no-capacity", action="store_true",
                   help="skip the capacity checks (resting room, collateral, shard cash, scan budget, restarts)")
    x.add_argument("--json")
    x.set_defaults(fn=cmd_preflight)
    x = sub.add_parser("pickoffs", help="adding fills on wave markets marked to the book (ours out); exit 3 = new")
    x.add_argument("name", nargs="?", default="all")
    x.add_argument("--hours", type=float, default=6.0)
    x.add_argument("--min-c", type=float, default=5.0, help="cents against us to flag (default 5)")
    x.add_argument("--min-usd", type=float, default=25.0, help="$ against us to flag (default 25)")
    x.add_argument("--series-trip", type=int, default=3, help="flagged markets in one series = a series trip")
    x.add_argument("--act", action="store_true",
                   help="pin each flagged registry-wave market (and tripped series) at the wave's base size, re-tune now")
    x.add_argument("--update-state", action="store_true")
    x.add_argument("--top", type=int, default=15)
    x.add_argument("--json")
    x.set_defaults(fn=cmd_pickoffs)
    x = sub.add_parser("darkcheck", help="listed wave markets vs what rests on Kalshi, with the reason; exit 3 = new dark")
    x.add_argument("--min-age", type=float, default=20.0, help="minutes into a listing before it is judged")
    x.add_argument("--act", action="store_true",
                   help="the old-wave lock case: re-tune its registry wave now, or adopt the series")
    x.add_argument("--update-state", action="store_true")
    x.add_argument("--json")
    x.set_defaults(fn=cmd_darkcheck)

    # PACE / RESIZE / WHYDARK (pvd_wave_pace.py; 2026-11-14, the short waves' questions)
    def _pace_mod(fn_name):
        def run(a):
            import pvd_wave_pace
            return getattr(pvd_wave_pace, fn_name)(a)
        return run
    x = sub.add_parser("pace", help="each listed market's period projection on its average share since its size "
                                    "landed; under $1 by the multiple it needs, side cost and series (read-only)")
    x.add_argument("name")
    x.add_argument("--aim", type=float, default=1.20, help="the projection to size for (Jack 11/11: $1.20)")
    x.add_argument("--max-mult", type=float, default=5.0)
    x.add_argument("--real", type=float, default=0.65, help="the share of the modelled gain counted (55-75%% measured)")
    x.add_argument("--window-min", type=float, default=60.0, help="minutes of cycle rows to average")
    x.add_argument("--json")
    x.set_defaults(fn=_pace_mod("cmd_pace"))
    x = sub.add_parser("resize", help="size markets under the aim up to --max-mult, off where that can't reach $1 "
                                      "(two reads); dry run unless --apply (Jack's word; pre-approved on type B waves with a live "
                                      "fair)")
    x.add_argument("name")
    x.add_argument("--aim", type=float, default=1.20)
    x.add_argument("--max-mult", type=float, default=5.0)
    x.add_argument("--real", type=float, default=0.65)
    x.add_argument("--window-min", type=float, default=60.0)
    x.add_argument("--keep-within", type=float, default=0.05,
                   help="a market projecting within this of $1 is kept, never turned off (estimate noise)")
    x.add_argument("--no-off", action="store_true", help="size up only; turn nothing off")
    x.add_argument("--apply", action="store_true", help="write the multiples file and run the wave now")
    x.add_argument("--force", action="store_true", help="apply even within 15 min of a restart or on a stale read")
    x.set_defaults(fn=_pace_mod("cmd_resize"))
    x = sub.add_parser("whydark", help="every side of a wave's listed markets not resting on Kalshi, with its "
                                       "reason (read-only)")
    x.add_argument("name", nargs="?", default="all")
    x.add_argument("--json")
    x.set_defaults(fn=_pace_mod("cmd_whydark"))

    x = sub.add_parser("datefix", help="event-start overrides for listed events the bot misreads (DDMONYY tickers)")
    x.add_argument("name", nargs="?", default="all")
    x.add_argument("--write", action="store_true")
    x.set_defaults(fn=cmd_datefix)
    x = sub.add_parser("run")
    x.add_argument("--write", action="store_true")
    x.add_argument("--only")
    x.add_argument("--wait-lock", type=float,
                   help="seconds to wait for another pass of a wave (default 600 with --only, else 0 = skip it)")
    x.set_defaults(fn=cmd_run)
    x = sub.add_parser("dash")
    x.add_argument("name")
    x.set_defaults(fn=cmd_dash)
    x = sub.add_parser("report")
    x.add_argument("name")
    x.add_argument("--detect")
    x.add_argument("--out")
    x.add_argument("--email", action="store_true",
                   help="also email it (the bot's alert mailer: the daily emails' sender and recipient)")
    x.add_argument("--test", action="store_true", help="with --email: [TEST] in the subject, a banner on top")
    x.set_defaults(fn=cmd_report)
    x = sub.add_parser("setarg")
    x.add_argument("name")
    x.add_argument("flag")
    x.add_argument("value", nargs="?")
    x.add_argument("--unset", action="store_true")
    x.add_argument("--group", choices=["main", "near"], default="main")
    x.set_defaults(fn=cmd_setarg)
    x = sub.add_parser("set")
    x.add_argument("name")
    x.add_argument("key")
    x.add_argument("value")
    x.set_defaults(fn=cmd_set)
    x = sub.add_parser("list")
    x.set_defaults(fn=cmd_list)
    x = sub.add_parser("remove")
    x.add_argument("name")
    x.set_defaults(fn=cmd_remove)
    x = sub.add_parser("stop", help="turn a wave off: disable it and move its live entries to 'stopped'")
    x.add_argument("name")
    x.add_argument("--reason", default="turned off")
    x.add_argument("--dry-run", action="store_true")
    x.set_defaults(fn=cmd_stop)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
