"""One-off: put the 12 pruned KXVENUEPERFORM-REDROCKS28JAN01 period-accrual
counters back into imm_state.json during the bot's restart window.

Values = the dt-integral of est_frac x pool_per_day from the cycle logs since
the live period start (2026-09-17 21:02Z); the same reconstruction matched the
23 surviving counters to the cent (2026-09-25 diagnosis).

Waits for the launcher's "bot exited" line (the bot self-exits on the source
change, the launcher restarts it 30s later; in that window nothing writes the
state file), backs the file up, patches, and exits. Existing counters are
never overwritten.
"""
import json
import os
import sys
import time
import datetime as dt

LOGDIR = r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm"
STATE = os.path.join(LOGDIR, "imm_state.json")
KEY = "KXVENUEPERFORM-REDROCKS28JAN01"
PERIOD_START = "2026-09-17T21:01:58.189141+00:00"   # live feed, isoformat as the bot stores it
RECON = {   # this-period accrual, $ (cycle-log reconstruction, 2026-09-25 ~14:00Z)
    "BLU": 0.547, "CHR": 0.701, "DOM": 0.717, "FRE": 0.769, "JOE": 0.992,
    "KAC": 0.720, "LOR": 0.618, "NOA": 0.659, "PRE": 0.788, "TAM": 0.820,
    "VAM": 0.958, "WID": 0.652,
}


def today_log():
    return os.path.join(LOGDIR, "incentive-mm-%s.log" % dt.date.today().strftime("%Y-%m-%d"))


def wait_for_exit(timeout_s=1800):
    """Block until a NEW 'launcher: bot exited' line appears in today's log."""
    path = today_log()
    start_size = os.path.getsize(path) if os.path.exists(path) else 0
    t0 = time.time()
    print("waiting for bot exit; log", path, "size", start_size, flush=True)
    while time.time() - t0 < timeout_s:
        path = today_log()
        if os.path.exists(path):
            size = os.path.getsize(path)
            if size > start_size:
                with open(path, "rb") as f:
                    f.seek(start_size)
                    new = f.read().decode("utf-8", "replace")
                if "launcher: bot exited" in new:
                    return True
        time.sleep(0.5)
    return False


def patch():
    with open(STATE, encoding="utf-8") as f:
        d = json.load(f)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(LOGDIR, "imm_state_backup_%s_pre_redrocks_restore.json" % stamp)
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(d, f)
    acc = d.setdefault("accrued_est", {})
    base = d.setdefault("period_base", {})
    ps = d.setdefault("period_start", {})
    done, skipped = [], []
    for sfx, v in RECON.items():
        t = "%s-%s" % (KEY, sfx)
        if t in acc:
            skipped.append((t, acc[t]))
            continue
        acc[t] = round(v, 4)
        base[t] = 0.0
        ps[t] = PERIOD_START
        done.append((t, v))
    tmp = STATE + ".restore.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, STATE)
    print("backup:", backup)
    print("restored %d counter(s): %s" % (len(done), ", ".join("%s=%.3f" % (t.split("-")[-1], v) for t, v in done)))
    print("skipped (already present): %s" % ", ".join("%s=%.3f" % (t.split("-")[-1], v) for t, v in skipped))


if __name__ == "__main__":
    if "--now" not in sys.argv:
        if not wait_for_exit():
            print("timed out waiting for the bot exit; nothing patched")
            sys.exit(2)
        t0 = time.time()
        patch()
        print("patched %.1fs after the exit line" % (time.time() - t0))
    else:
        patch()
