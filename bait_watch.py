"""Bait watch (Jack 2026-11-11: "Be extremely careful for baiters, notify me when
something suspicious occurs", then "Just cover market wave"). Every 15 min (the relight
task's pass 10) it reads what the bot's own bait defenses logged since the last check on
MARKET-WAVE markets (registry waves and the list), and reports what is new. Their fills
that went against us are pass 8's (the pick-off watch); this adds the rest. Read-only on
the bot and the list.

  python bait_watch.py [--since "YYYY-MM-DD HH:MM:SS"] [--update-state] [--json OUT]

What counts, and how loud (wave_alert.py bait sends it):
  TEXT + email
    bait stop     the bot's bait-fingerprint stop fired: a taker of exactly our size took
                  our whole rung on a thin top our quote had jumped to (that side is out
                  until 00:00 ET).
    swept 3x      the sweep breaker's 3rd trip within 30 min in one event (60-min hold):
                  someone keeps taking every order we rest there.
  EMAIL only
    toxic halt    the bot paused a side picked off twice in 24 h.
    swept 2x      the sweep breaker's 2nd trip within 30 min (30-min hold).
    flash         on one market side, a better price shown and pulled 3+ times in 60 min
                  before we joined it (the join wait held us back each time).
Not alerted (too common to mean bait on their own; the defenses act on them silently):
the lone-reference guard standing a side aside, one sweep, one pulled price, the twin
check (it also re-prices 2-candidate nominee events that aren't D/R pairs).

Exit 3 = something new; 0 = nothing. State: run-logs/pvd-scan/bait_watch_state.json
(what was reported, so nothing is reported twice; a flashing market side re-alerts after
6 h, or sooner after 3 more flashes).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
INC = os.path.join(HERE, "run-logs", "incentive-mm")
STATE = os.environ.get("BAIT_WATCH_STATE") or os.path.join(HERE, "run-logs", "pvd-scan", "bait_watch_state.json")
TAIL_BYTES = 12_000_000          # ~4-5 h of bot log: covers the flash window and a missed run
FLASH_N, FLASH_MIN = 3, 60
REALERT_H = 6.0
KEEP_H = 48.0

TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)Z"
RX_BAIT = re.compile(TS + r" .*?bait stop( \(dry\))?: (\S+) (BID|ASK) @ ([\d.]+)c -- a taker of exactly our "
                     r"([\d.]+)")
RX_TOXIC = re.compile(TS + r" .*?ALERT \[toxic_halt\] (\S+) (BID|ASK) picked off (\d+)x in 24h "
                      r"\(latest: filled ([\d.]+)c, mark ([\d.]+)c (\d+)min later\)")
RX_SWEEP = re.compile(TS + r" .*?sweep breaker \(on\): (\S+) (BID|ASK) @ ([\d.]+)c filled out \(([\d.]+)\) "
                      r"-> pulling (\d+) order\(s\) in (\S+) for (\d+)s(?:; trip (\d+))?")
RX_WENT = re.compile(TS + r" .*?join wait (\S+) (bid|ask): the better price went away after (\d+)s; "
                     r"rests ([\d.]+)c")


def _dt(s: str) -> datetime:
    return datetime.strptime(s[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _z(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


def log_lines(tail_bytes: int = TAIL_BYTES) -> list:
    """The last `tail_bytes` of the newest bot log (and the one before it, when the newest
    is shorter), oldest first."""
    logs = sorted(glob.glob(os.path.join(INC, "incentive-mm-20*.log")), key=os.path.getmtime)[-2:]
    out, budget = [], tail_bytes
    for p in reversed(logs):
        if budget <= 0:
            break
        try:
            size = os.path.getsize(p)
            with open(p, "rb") as fh:
                if size > budget:
                    fh.seek(size - budget)
                    fh.readline()
                data = fh.read()
        except OSError:
            continue
        budget -= len(data)
        out = data.decode("utf-8", "replace").splitlines() + out
    return out


def wave_lookup():
    """ticker -> wave name (None off the waves), the pick-off watch's own test."""
    sys.path.insert(0, HERE)
    import pvd_waves as pw
    reg = pw.load()
    try:
        with open(pw.LIST, encoding="utf-8") as fh:
            L = json.load(fh).get("markets") or {}
    except (OSError, ValueError):
        L = {}
    legacy = pw.legacy_series()
    memo: dict = {}

    def wave(t: str):
        if t not in memo:
            memo[t] = pw.wave_of(t, reg, L, legacy)[0]
        return memo[t]
    return wave


def scan_log(lines, since: str, now: datetime, wave) -> list:
    """Bait signals on wave markets in the log lines; each item carries `key` (its
    identity), `level` and `wave`."""
    items = []
    flash = defaultdict(list)
    lo_flash = _z(now - timedelta(minutes=FLASH_MIN))
    for ln in lines:
        if "bait stop" in ln:
            m = RX_BAIT.search(ln)
            if m and m.group(1) > since and wave(m.group(3)):
                items.append({"kind": "bait stop", "level": "text", "ts": m.group(1), "ticker": m.group(3),
                              "side": m.group(4).lower(), "px": float(m.group(5)), "n": float(m.group(6)),
                              "dry": bool(m.group(2)), "wave": wave(m.group(3)),
                              "key": f"bait|{m.group(3)}|{m.group(4)}|{m.group(1)}"})
        elif "toxic_halt" in ln:
            m = RX_TOXIC.search(ln)
            if m and m.group(1) > since and wave(m.group(2)):
                items.append({"kind": "toxic halt", "level": "email", "ts": m.group(1), "ticker": m.group(2),
                              "side": m.group(3).lower(), "times": int(m.group(4)), "px": float(m.group(5)),
                              "mark": float(m.group(6)), "after_min": int(m.group(7)), "wave": wave(m.group(2)),
                              "key": f"toxic|{m.group(2)}|{m.group(3)}|{m.group(1)}"})
        elif "sweep breaker (on)" in ln:
            m = RX_SWEEP.search(ln)
            trip = int(m.group(9) or 1) if m else 1
            if m and m.group(1) > since and trip >= 2 and wave(m.group(2)):
                items.append({"kind": f"swept {trip}x", "level": "text" if trip >= 3 else "email",
                              "ts": m.group(1), "ticker": m.group(2), "side": m.group(3).lower(),
                              "px": float(m.group(4)), "n": float(m.group(5)), "event": m.group(7),
                              "pulled": int(m.group(6)), "hold_min": int(m.group(8)) // 60, "trip": trip,
                              "wave": wave(m.group(2)), "key": f"sweep|{m.group(7)}|{m.group(1)}"})
        elif "the better price went away" in ln:
            m = RX_WENT.search(ln)
            if m and m.group(1) >= lo_flash:
                flash[(m.group(2), m.group(3))].append(m.group(1))
    for (t, side), tss in flash.items():
        if len(tss) >= FLASH_N and max(tss) > since and wave(t):
            items.append({"kind": "flash", "level": "email", "ts": max(tss), "ticker": t, "side": side,
                          "times": len(tss), "first": min(tss), "wave": wave(t), "key": f"flash|{t}|{side}"})
    return items


def pick_new(items: list, sent: dict, now: datetime) -> list:
    """Items not reported yet: a one-off event (bait stop, toxic, sweep) once; a flashing
    market side again after REALERT_H, or sooner after FLASH_N more flashes."""
    new = []
    for it in items:
        prev = sent.get(it["key"])
        if prev is None:
            new.append(it)
        elif it["kind"] == "flash":
            age_h = (now - _dt(prev["at"])).total_seconds() / 3600
            if age_h >= REALERT_H or (it.get("times") or 0) >= (prev.get("times") or 0) + FLASH_N:
                new.append(it)
    return new


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help='"YYYY-MM-DD HH:MM:SS" (UTC); default: the last check (a first run: 15 min)')
    ap.add_argument("--update-state", action="store_true")
    ap.add_argument("--json")
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    since = a.since or st.get("last") or _z(now - timedelta(minutes=15))
    items = scan_log(log_lines(), since, now, wave_lookup())
    sent = st.get("sent") or {}
    new = pick_new(items, sent, now)
    out = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "since": since, "items": items, "new": new,
           "text": any(i["level"] == "text" for i in new)}
    print(f"bait watch {out['at']} since {since}Z (wave markets): {len(items)} signal(s), {len(new)} new"
          + (" (TEXT)" if out["text"] else ""))
    for it in new:
        print(f"  NEW {it['kind']}: {it['ticker']} {it.get('side', '')} at {it['ts']}Z [{it['wave']}, {it['level']}]")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    if a.update_state:
        for it in new:
            sent[it["key"]] = {"at": out["at"], "times": it.get("times")}
        keep = now - timedelta(hours=KEEP_H)
        sent = {k: v for k, v in sent.items() if _dt(v["at"]) >= keep}
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"last": _z(now), "sent": sent}, f)
        os.replace(STATE + ".tmp", STATE)
    return 3 if new else 0


if __name__ == "__main__":
    sys.exit(main())
