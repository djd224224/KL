"""Relight watch (Jack 2026-11-08: "Keep eyes peeled for relighting of existing
wave or any new waves"). The relight tasks ("KL pvd-scan" every 15 min at
:07/:22/:37/:52, "KL pvd-scan waves" at :00/:15/:30/:45) already list relit and
new programs of the waves on their own; this reads their logs since the last
check and says what they listed, and how Kalshi's live program count moved
(a jump means a batch of programs relit, whether or not any cleared the bar).

  python relight_watch.py [--since "2026-11-08 05:00:00Z"] [--update-state] [--json OUT]

Exit 3 = something was listed (or the program count jumped by --jump or more)
since the last check; 0 = nothing. State: run-logs/pvd-scan/relight_watch_state.json.
Read-only on the list and the bot: it only reads logs.
"""
import argparse
import ast
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = [os.path.join(HERE, "run-logs", "pvd-scan", "waves-task.log"),
        os.path.join(HERE, "run-logs", "pvd-scan", "scan-task.log")]
STATE = os.path.join(HERE, "run-logs", "pvd-scan", "relight_watch_state.json")
# the wave registry and the list: a NEW wave the 24/7 wave check lists (pvd_waves.py add
# + run) writes no "taking N by ROI" line to the relight tasks' logs, so its program jump
# read as "0 listed" (2026-11-11 16:22Z: +803 programs = w1111-statenom, 654 mkts listed)
REGISTRY = os.path.join(HERE, "run-logs", "pvd-scan", "waves.json")
LIST = os.path.join(HERE, "run-logs", "incentive-mm", "post_vote_dead_books.json")
TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ)"
RX_TAKE = re.compile(TS + r" .*?taking (\d+) by ROI \(extended (\d+), relit (\d+), new (\d+)\)"
                     r"(?:.*?~\$([\d,.]+)/day)?")
RX_PROG = re.compile(TS + r" programs (\d+)")
RX_PASS = re.compile(r"^=+ .*?pass (\d+): (.*?)\s*$")
RX_WAVE = re.compile(r"^== wave (\S+)(?: (main|near)\b)?")
RX_WROTE = re.compile(TS + r" WROTE (\d+) entries .*?\(now (\d+) listed\)")
RX_SKIP = re.compile(r"skipped (\{[^}]*\})")    # the scan's left-out markets by reason (wave_alert.py)


def scan(since: str):
    takes, progs = [], []
    for path in LOGS:
        src = "waves task" if "waves" in os.path.basename(path) else "election scan"
        label = src
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                m = RX_PASS.match(line)
                if m:
                    label = f"pass {m.group(1)}: {m.group(2)}"
                    continue
                m = RX_WAVE.match(line)
                if m:
                    label = f"wave {m.group(1)}" + (f" {m.group(2)}" if m.group(2) else "")
                    continue
                m = RX_PROG.search(line)
                if m and m.group(1) > since:
                    progs.append((m.group(1), int(m.group(2))))
                m = RX_TAKE.search(line)
                if m and m.group(1) > since and int(m.group(2)) > 0:
                    takes.append({"ts": m.group(1), "where": f"{src} / {label}",
                                  "taken": int(m.group(2)), "extended": int(m.group(3)),
                                  "relit": int(m.group(4)), "new": int(m.group(5)),
                                  "usd_day": float((m.group(6) or "0").replace(",", ""))})
                    k = RX_SKIP.search(line)
                    if k:
                        try:
                            takes[-1]["skipped"] = {str(a): int(b) for a, b in ast.literal_eval(k.group(1)).items()}
                        except (ValueError, SyntaxError):
                            pass
                m = RX_WROTE.search(line)
                if m and m.group(1) > since and takes and takes[-1]["ts"][:16] == m.group(1)[:16]:
                    takes[-1]["now_listed"] = int(m.group(3))
    progs.sort()
    takes.sort(key=lambda r: r["ts"])
    return takes, progs


def new_waves(since: str, until: str = "9999") -> list:
    """Registry waves added in (since, until] -- the wave check's own listings --
    each with the markets the list now holds for its series and their est $/day."""
    try:
        reg = json.load(open(REGISTRY, encoding="utf-8")).get("waves") or {}
    except (OSError, ValueError, AttributeError):
        return []
    lo, hi = since.replace(" ", "T"), until.replace(" ", "T")
    fresh = {n: w for n, w in reg.items()
             if isinstance(w, dict) and lo < str(w.get("added") or "") <= hi}
    if not fresh:
        return []
    try:
        listed = json.load(open(LIST, encoding="utf-8")).get("markets") or {}
    except (OSError, ValueError, AttributeError):
        listed = {}
    out = []
    for name, w in sorted(fresh.items(), key=lambda kv: kv[1].get("added") or ""):
        ser = set(w.get("series") or []) | set(w.get("near") or [])
        mk = [e for t, e in listed.items() if t.split("-")[0] in ser]
        out.append({"name": name, "added": w.get("added"), "series": sorted(ser), "markets": len(mk),
                    "usd_day": round(sum(float(e.get("est_usd_day") or 0) for e in mk), 2),
                    "note": w.get("note") or ""})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", help='"YYYY-MM-DD HH:MM:SSZ"; default: the last check (state file)')
    ap.add_argument("--update-state", action="store_true")
    ap.add_argument("--jump", type=int, default=500,
                    help="a live-program count rise this big counts as a relight (default 500)")
    ap.add_argument("--json")
    a = ap.parse_args()
    st = {}
    try:
        st = json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        pass
    since = a.since or st.get("last_ts") or "0000"
    takes, progs = scan(since)
    p0 = st.get("last_programs")
    p_now = progs[-1][1] if progs else p0
    p_max = max([p for _t, p in progs] or [p_now or 0])
    jump = (p_max - p0) if (p0 is not None and p_max is not None) else 0
    n = sum(t["taken"] for t in takes)
    usd = sum(t["usd_day"] for t in takes)
    print(f"relight watch since {since}: {n} market(s) listed in {len(takes)} pass(es), ~${usd:,.0f}/day; "
          f"live programs {p0 if p0 is not None else '?'} -> {p_now if p_now is not None else '?'}"
          + (f" (peak {p_max}, +{jump})" if jump > 0 else ""))
    for t in takes:
        print(f"  {t['ts']}  {t['where']}: {t['taken']} listed (relit {t['relit']}, new {t['new']}, "
              f"extended {t['extended']}), ~${t['usd_day']:,.0f}/day"
              + (f", now {t['now_listed']} on the list" if "now_listed" in t else ""))
    last_ts = max([t["ts"] for t in takes] + [p[0] for p in progs] + [since])
    waves = new_waves(since)
    for w in waves:
        print(f"  new wave {w['name']} added {w['added']} by the wave check: {w['markets']} mkts on the list "
              f"(~${w['usd_day']:,.0f}/day), {len(w['series'])} series")
    out = {"since": since, "until": last_ts, "listed": n, "usd_day": round(usd, 2), "passes": takes,
           "programs_before": p0, "programs_now": p_now, "programs_jump": jump, "new_waves": waves}
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)
    if a.update_state:
        with open(STATE + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"last_ts": last_ts, "last_programs": p_now}, f)
        os.replace(STATE + ".tmp", STATE)
    return 3 if (n > 0 or jump >= a.jump) else 0


if __name__ == "__main__":
    sys.exit(main())
