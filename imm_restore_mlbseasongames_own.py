"""One-off: put KXMLBSEASONGAMES-27-2425 back to the bot's own -25 @ 38c in
imm_state.json during the bot's restart window.

The 2026-09-30 03:17Z startup reconcile -- before its ownership gate --
adopted 22.36 contracts of Jack's own manual fill (order 01a0e5b3-a9c0, no
client_order_id, 9/29 18:59Z) as the bot's: own -25 @ 38 became -47.36 @
42.5. Kalshi's fills on the bot's orders say -25 @ 38 (a 25-lot YES sell at
38c on 9/27).

The patch must land on a restart onto the gated code, or the next startup
reconcile adopts the fill again. So it waits for the launcher's "bot exited"
line (the bot self-exits on the source change and the launcher restarts it
30s later; nothing writes the state file in that window), checks the live
tree contains --require-commit, backs the state file up, patches only while
the entry still holds the adopted values, verifies, and exits.

    python imm_restore_mlbseasongames_own.py --require-commit <gate sha>
    python imm_restore_mlbseasongames_own.py --dry-run     # check, write nothing
"""
import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import time

LIVE = r"C:\Users\jackd\Documents\KL"
LOGDIR = os.path.join(LIVE, "run-logs", "incentive-mm")
STATE = os.path.join(LOGDIR, "imm_state.json")
TICKER = "KXMLBSEASONGAMES-27-2425"
ADOPTED_POS = -47.36          # what the 9/30 03:17Z reconcile wrote
OWN_POS, OWN_AVG = -25.0, 38.0


def recent_logs():
    """The launcher names each run's log by the LOCAL date at the run's start
    and appends the 'bot exited' line to that same file, so a run that began
    before midnight exits into yesterday's file: watch the newest two."""
    return sorted(glob.glob(os.path.join(LOGDIR, "incentive-mm-????-??-??.log")))[-2:]


def live_tree_has(sha):
    r = subprocess.run(["git", "-C", LIVE, "merge-base", "--is-ancestor", sha, "HEAD"],
                       capture_output=True, text=True, timeout=30)
    return r.returncode == 0


def wait_for_exit_on(sha, timeout_s):
    """Block until a NEW 'launcher: bot exited' line appears while the live
    tree contains `sha`. An exit before the sync pulls `sha` is skipped."""
    start = {p: os.path.getsize(p) for p in recent_logs()}
    t0 = time.time()
    print("waiting for a bot exit on %s; watching %s" % (
        sha[:9], ", ".join(os.path.basename(p) for p in start)), flush=True)
    while time.time() - t0 < timeout_s:
        for path in recent_logs():
            base = start.setdefault(path, 0)
            size = os.path.getsize(path) if os.path.exists(path) else 0
            if size <= base:
                continue
            with open(path, "rb") as f:
                f.seek(base)
                new = f.read(size - base).decode("utf-8", "replace")
            start[path] = size
            if "launcher: bot exited" in new:
                if live_tree_has(sha):
                    return True
                print("bot exited before the live tree had %s; waiting for the next exit"
                      % sha[:9], flush=True)
        time.sleep(0.5)
    return False


def patch(dry_run):
    try:
        os.remove(STATE + ".restore.tmp")     # a stale leftover from a failed swap
    except OSError:
        pass
    with open(STATE, encoding="utf-8") as f:
        d = json.load(f)
    pos = (d.get("own_pos") or {}).get(TICKER)
    avg = (d.get("own_avg") or {}).get(TICKER)
    print("state now: own_pos %r own_avg %r" % (pos, avg), flush=True)
    if pos is None or abs(float(pos) - ADOPTED_POS) > 0.01:
        print("own_pos is not the adopted %.2f; nothing patched (check by hand)" % ADOPTED_POS)
        sys.exit(3)
    if dry_run:
        print("dry run: would set own_pos %.2f, own_avg %.2f" % (OWN_POS, OWN_AVG))
        return
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(LOGDIR, "imm_state_backup_%s_pre_mlbseasongames_restore.json" % stamp)
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(d, f)
    d["own_pos"][TICKER] = OWN_POS
    d.setdefault("own_avg", {})[TICKER] = OWN_AVG
    tmp = STATE + ".restore.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    # Windows: a concurrent reader (a status task, the dashboard) holding the
    # state file open makes the atomic swap fail with PermissionError. Retry
    # for a few seconds, then write the content straight into the file -- not
    # atomic, but the bot is down in this window.
    swapped = False
    for _ in range(20):
        try:
            os.replace(tmp, STATE)
            swapped = True
            break
        except PermissionError as e:
            print("swap blocked (%s); retrying" % e, flush=True)
            time.sleep(0.5)
    if not swapped:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f)
        try:
            os.remove(tmp)
        except OSError:
            pass
        print("wrote the state file directly (swap kept failing)")
    with open(STATE, encoding="utf-8") as f:
        check = json.load(f)
    got = ((check.get("own_pos") or {}).get(TICKER), (check.get("own_avg") or {}).get(TICKER))
    if got != (OWN_POS, OWN_AVG):
        print("VERIFY FAILED: state holds %r after the write" % (got,))
        sys.exit(4)
    print("backup:", backup)
    print("restored %s: own_pos %.2f, own_avg %.2f" % (TICKER, OWN_POS, OWN_AVG))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--require-commit", help="the gate commit the restart must run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout-min", type=float, default=120.0)
    a = ap.parse_args()
    if a.dry_run:
        patch(True)
        sys.exit(0)
    if not a.require_commit:
        ap.error("--require-commit is required (the patch must land on the gated code)")
    if not wait_for_exit_on(a.require_commit, a.timeout_min * 60):
        print("timed out waiting for a bot exit on the gated code; nothing patched")
        sys.exit(2)
    t0 = time.time()
    patch(False)
    print("patched %.1fs after the exit line" % (time.time() - t0))
