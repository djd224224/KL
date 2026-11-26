#!/usr/bin/env python3
"""imm_log_retention.py -- compress the IMM's old text logs in place.

Review R-261124-03 (Jack 2026-11-25: "Yes, 35 days"). run-logs/incentive-mm
grew about 2.4 GB a day with nothing compressing it, so the disk would fill
in early January. This gzips the bot's text logs once they are 35 days old,
now that the tools that read old days open .gz through imm_logfiles. Nothing
is deleted. Each file is verified byte for byte before its plain copy goes,
each one gets a manifest line, and `restore` gives the originals back. Data
here only changes on purpose and never silently.

WHAT (an allowlist; nothing else is touched)
  <prefix>_<UTC day>.<ext> for the prefixes in ALLOW, plus the bot's console
  logs incentive-mm-<date>.log.
  NEVER: fills (the bot itself reads the last 30 days of fills for its
  marks), settlements, realized, book_depth (already gzip), state files,
  caches, anything not in ALLOW.

WHEN
  * A UTC-day file once its day is more than CUTOFF_DAYS old. CUTOFF_DAYS is
    the dashboard's IMM_DASH_HISTORY_DAYS (30) + 5: the dashboard's
    10-minute build rewrites its last 30 days of day archive from these raw
    files, and reads the day before, so they stay plain well past that.
  * And only once both ET days that read it are in the dashboard's day
    archive. UTC day d holds the evening of ET day d-1 and the rest of ET d.
  * A console log goes by its LAST write, once that is CUTOFF_DAYS old: the
    launcher names a log for the day it started and keeps writing to it past
    midnight.
  * Never a file written in the last 2 days, or one another program holds
    open.

HOW (one file at a time, at below-normal priority)
  X -> X.gz.tmp, at gzip level 6, with X's name and mtime in the header.
  Then it reads the .tmp back and compares the decompressed size + sha1
  with what it read from X. It gives the .tmp X's timestamps,
  os.replace()s it to X.gz, and deletes X. Any mismatch deletes the .tmp
  and keeps X.
  An X.gz that already exists is never overwritten. If it holds exactly
  X's bytes, X is deleted; otherwise the run stops and reports.
  .tmp files left by a killed run are removed.
  Because the .gz keeps X's mtime and gzip records X's size, imm_logfiles.sig()
  reads the same and the readers' caches carry over.

CONTROLS
  (no flag)            a dry run: lists what it would compress, changes nothing
  --apply              compress
  --not-before DATE    with --apply: still a dry run before that local (ET) date
  --max-gb N           input cap per run (default 10)
  --max-minutes N      starts no new file after N minutes (default 90)
  IMM_LOG_RETENTION=0, or a file named STOP in the retention folder: off
  --apply refuses unless every tool in READERS imports imm_logfiles
  restore PATH.gz ...  |  restore --all
                       undo: the originals back, checked against the manifest.
                       Create the STOP file first, or the next run
                       compresses them again.

OUTPUT (run-logs/incentive-mm/retention/)
  manifest.jsonl   one line per file changed: compressed, removed_plain,
                   plain_held, restored, failed, conflict
  last_run.json    the run's summary, read by the daily review
  dry_run.txt      what a dry run would do
  run.log          this script's lines (ASCII: the task console is cp1252)
Exit codes: 0 ok (a dry run or off included), 1 a file failed,
2 a .gz conflict, 3 refused (the readers are not ready), 4 another run holds
the lock.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_DIR = os.environ.get("IMM_STATUS_DIR", os.path.join(HERE, "run-logs", "incentive-mm"))
HISTORY_DAYS = int(os.environ.get("IMM_DASH_HISTORY_DAYS", "30"))   # imm_dashboard's
MARGIN_DAYS = 5
CUTOFF_DAYS = HISTORY_DAYS + MARGIN_DAYS
RECENT_SECS = 2 * 86400
LOCK_STALE_SECS = 3 * 3600
CHUNK = 1 << 20
LEVEL = 6
GB = float(2 ** 30)

# prefix -> extension. An allowlist: a new sink stays plain until it is added here.
ALLOW = {
    "cycle_log": "csv", "orders": "jsonl", "marks": "jsonl", "selection_snapshot": "jsonl",
    "selection_events": "jsonl", "floor_state": "jsonl", "guard_skips": "jsonl",
    "back_of_line": "jsonl", "ws_stale": "jsonl", "risk_dry": "jsonl",
    # imm_quote_gaps' scan archive (review R-261124-24, ~3 MB a day); its one
    # reader, imm_review.gap_scans_of_day, opens it through imm_logfiles
    "gap_scans": "jsonl",
    # imm_fair_marks.py's model history (11/26); its reader, imm_fair_scorecard,
    # opens it through imm_logfiles
    "fair_marks": "jsonl", "fair_results": "jsonl",
}
NEVER = ("fills", "settlements", "realized", "book_depth")
assert not set(ALLOW) & set(NEVER)
# The tools that read days older than the cutoff; --apply refuses unless each
# one opens day files through imm_logfiles.
READERS = ("imm_reward_recon.py", "imm_saturday_tracker.py", "imm_dashboard.py", "imm_review.py",
           "imm_fair_scorecard.py")

_DAY_RX = re.compile(r"^([a-z][a-z_]*?)_(\d{4}-\d{2}-\d{2})\.(jsonl|csv)$")
_BOT_RX = re.compile(r"^incentive-mm-(\d{4}-\d{2}-\d{2})\.log$")
_LOG_FH = None


# ---- small helpers ------------------------------------------------------------

def log(msg: str) -> None:
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    line = line.encode("ascii", "replace").decode("ascii")
    print(line, flush=True)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except OSError:
            pass


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _gb(n: float) -> float:
    return round(n / GB, 3)


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def archive_dir_default(status_dir: str) -> str:
    """imm_dashboard's day archive: IMM_DASH_DAY_ARCHIVE, else
    <IMM_DASH_DIR or STATUS_DIR/dashboard>/cache/day_archive."""
    return (os.environ.get("IMM_DASH_DAY_ARCHIVE")
            or os.path.join(os.environ.get("IMM_DASH_DIR") or os.path.join(status_dir, "dashboard"),
                            "cache", "day_archive"))


def archived_days(archive_dir: str) -> set:
    try:
        return {n[:10] for n in os.listdir(archive_dir) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", n)}
    except OSError:
        return set()


def readers_missing(code_dir: str = HERE) -> list:
    """READERS that do not import imm_logfiles (or cannot be read)."""
    out = []
    for name in READERS:
        try:
            with open(os.path.join(code_dir, name), encoding="utf-8", errors="replace") as f:
                if "import imm_logfiles" not in f.read():
                    out.append(name)
        except OSError:
            out.append(name)
    return out


def held_open(path: str) -> bool:
    """True when another program has the file open. On Windows an exclusive
    open then fails with a sharing violation; elsewhere this returns False and
    the final delete is the check."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CreateFileW.restype = wintypes.HANDLE
        h = k32.CreateFileW(path, 0x80000000, 0, None, 3, 0x80, None)   # GENERIC_READ, share none
        if h is None or h == ctypes.c_void_p(-1).value:
            return ctypes.get_last_error() == 32                          # ERROR_SHARING_VIOLATION
        k32.CloseHandle(h)
        return False
    except Exception:
        return False


def _below_normal() -> None:
    try:
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    except Exception:
        pass


def _digest(path: str, opener=open):
    """(bytes, sha1 hex) of a file's content; opener=gzip.open reads a .gz's."""
    h = hashlib.sha1()
    n = 0
    with opener(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            n += len(b)
    return n, h.hexdigest()


# ---- what to compress ---------------------------------------------------------

def scan(status_dir: str, archive_dir: str, now: float = None, cutoff_days: int = CUTOFF_DAYS):
    """(todo, held): the files a run would compress, oldest first, and the old
    ones it must leave for now, each {path, name, kind, day, size, mtime[, reason]}."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, timezone.utc).date()
    archived = archived_days(archive_dir)
    todo, held = [], []
    try:
        names = sorted(os.listdir(status_dir))
    except OSError:
        return [], []
    for name in names:
        path = os.path.join(status_dir, name)
        m = _DAY_RX.match(name)
        if m and ALLOW.get(m.group(1)) == m.group(3):
            kind, day = m.group(1), m.group(2)
            try:
                if (today - date.fromisoformat(day)).days <= cutoff_days:
                    continue
            except ValueError:
                continue
        else:
            b = _BOT_RX.match(name)
            if not b:
                continue
            kind, day = "bot_log", b.group(1)
        try:
            st = os.stat(path)
        except OSError:
            continue
        if kind == "bot_log" and now - st.st_mtime <= cutoff_days * 86400:
            continue                                    # by LAST write, never by name
        item = {"path": path, "name": name, "kind": kind, "day": day,
                "size": st.st_size, "mtime": st.st_mtime}
        if now - st.st_mtime < RECENT_SECS:
            held.append(dict(item, reason="written in the last 2 days"))
            continue
        if kind != "bot_log":
            d = date.fromisoformat(day)
            missing = [x for x in ((d - timedelta(days=1)).isoformat(), day) if x not in archived]
            if missing:
                held.append(dict(item, reason="ET day not in the day archive: " + " ".join(missing)))
                continue
        todo.append(item)
    todo.sort(key=lambda r: (r["day"], r["name"]))
    return todo, held


# ---- one file -----------------------------------------------------------------

def compress_file(path: str, level: int = LEVEL) -> dict:
    """Compress one file in place, verified. Returns {"action": compressed |
    removed_plain | plain_held | skipped | failed | conflict, ...}; raises
    OSError on I/O trouble (the .tmp is removed and the original kept)."""
    gz, tmp = path + ".gz", path + ".gz.tmp"
    st = os.stat(path)
    if os.path.exists(gz):
        n1, h1 = _digest(path)
        n2, h2 = _digest(gz, gzip.open)
        if (n1, h1) != (n2, h2):
            return {"action": "conflict", "size": n1, "sha1": h1,
                    "reason": f"{os.path.basename(gz)} already exists and holds different bytes"}
        os.remove(path)
        return {"action": "removed_plain", "size": n1, "sha1": h1, "gz_size": os.path.getsize(gz),
                "reason": "the .gz already held exactly these bytes"}
    h = hashlib.sha1()
    n = 0
    try:
        with open(path, "rb") as src, open(tmp, "wb") as raw:
            with gzip.GzipFile(filename=os.path.basename(path), mode="wb", compresslevel=level,
                               fileobj=raw, mtime=int(st.st_mtime)) as g:
                while True:
                    b = src.read(CHUNK)
                    if not b:
                        break
                    h.update(b)
                    n += len(b)
                    g.write(b)
            raw.flush()
            os.fsync(raw.fileno())
        st2 = os.stat(path)
        if n != st.st_size or (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            _rm(tmp)
            return {"action": "skipped", "reason": "changed while it was being compressed"}
        n2, h2 = _digest(tmp, gzip.open)
        if (n2, h2) != (n, h.hexdigest()):
            _rm(tmp)
            return {"action": "failed", "size": n, "sha1": h.hexdigest(),
                    "reason": "verify failed: the .gz does not give back the original bytes"}
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, gz)
    except BaseException:
        _rm(tmp)
        raise
    out = {"size": n, "sha1": h.hexdigest(), "gz_size": os.path.getsize(gz)}
    try:
        os.remove(path)
    except PermissionError:
        # held at the last moment: both copies stay, identical; the next run
        # re-verifies the pair and removes the plain one
        return dict(out, action="plain_held", reason="the plain copy was held open; removed next run")
    return dict(out, action="compressed")


def restore_file(gz: str, expect: dict = None) -> dict:
    """X.gz -> X, checked against the manifest's size + sha1 when known."""
    plain = gz[:-3]
    tmp = plain + ".restore.tmp"
    if os.path.exists(plain):
        n1, h1 = _digest(plain)
        n2, h2 = _digest(gz, gzip.open)
        if (n1, h1) != (n2, h2):
            return {"action": "conflict", "reason": f"{os.path.basename(plain)} exists and differs"}
        os.remove(gz)
        return {"action": "restored", "size": n1, "sha1": h1, "reason": "the plain copy was already there"}
    h = hashlib.sha1()
    n = 0
    try:
        with gzip.open(gz, "rb") as src, open(tmp, "wb") as out:
            while True:
                b = src.read(CHUNK)
                if not b:
                    break
                h.update(b)
                n += len(b)
                out.write(b)
            out.flush()
            os.fsync(out.fileno())
        if expect and (expect.get("size") != n or expect.get("sha1") != h.hexdigest()):
            _rm(tmp)
            return {"action": "failed", "size": n, "sha1": h.hexdigest(),
                    "reason": "does not match the manifest's size and sha1"}
        st = os.stat(gz)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, plain)
    except BaseException:
        _rm(tmp)
        raise
    os.remove(gz)
    return {"action": "restored", "size": n, "sha1": h.hexdigest()}


# ---- a run --------------------------------------------------------------------

def _manifest(out_dir: str, item: dict, res: dict, mode: str) -> None:
    row = {"ts": _iso(time.time()), "mode": mode, "action": res.get("action"), "file": item.get("name"),
           "kind": item.get("kind"), "day": item.get("day"), "size": res.get("size", item.get("size")),
           "gz_size": res.get("gz_size"), "sha1": res.get("sha1"),
           "mtime": _iso(item["mtime"]) if item.get("mtime") else None, "reason": res.get("reason")}
    with open(os.path.join(out_dir, "manifest.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _take_lock(out_dir: str) -> bool:
    path = os.path.join(out_dir, "run.lock")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) > LOCK_STALE_SECS:
            os.remove(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except OSError:
        return False


def _held_class(reason: str) -> str:
    return reason.split(":")[0]


def run(apply: bool = False, status_dir: str = None, archive_dir: str = None, out_dir: str = None,
        max_gb: float = 10.0, max_minutes: float = 90.0, not_before: str = None, now: float = None,
        today_local: str = None, code_dir: str = HERE, cutoff_days: int = CUTOFF_DAYS) -> int:
    """One run; returns the exit code. Writes last_run.json for every mode
    except "locked" (that summary belongs to the run holding the lock)."""
    global _LOG_FH
    t0 = time.time()
    status_dir = status_dir or STATUS_DIR
    archive_dir = archive_dir or archive_dir_default(status_dir)
    out_dir = out_dir or os.path.join(status_dir, "retention")
    os.makedirs(out_dir, exist_ok=True)
    _LOG_FH = open(os.path.join(out_dir, "run.log"), "a", encoding="ascii", errors="replace")
    summary = {"ts": _iso(time.time()), "version": 1, "mode": None, "status_dir": status_dir,
               "archive_dir": archive_dir, "cutoff_days": cutoff_days, "history_days": HISTORY_DAYS,
               "not_before": not_before, "max_gb": max_gb, "reason": None, "stopped": None,
               "candidates": 0, "candidates_gb": 0.0, "held": 0, "held_gb": 0.0, "held_reasons": {},
               "compressed": 0, "removed_plain": 0, "plain_held": 0, "in_gb": 0.0, "out_gb": 0.0,
               "freed_gb": 0.0, "skipped": {}, "failed": [], "conflicts": []}
    lock_taken = False
    try:
        if (os.environ.get("IMM_LOG_RETENTION", "1").strip() == "0"
                or os.path.exists(os.path.join(out_dir, "STOP"))):
            summary.update(mode="off", reason="IMM_LOG_RETENTION=0 or the STOP file")
            log("log retention is OFF (IMM_LOG_RETENTION=0 or the STOP file); nothing done")
            return 0
        mode = "apply" if apply else "dry-run"
        today_local = today_local or datetime.now().date().isoformat()
        if apply and not_before and today_local < not_before:
            mode = "dry-run"
            summary["reason"] = f"--apply waits until {not_before} (local date {today_local})"
        summary["mode"] = mode
        if mode == "apply":
            missing = readers_missing(code_dir)
            if missing:
                summary.update(mode="refused",
                               reason="readers that do not import imm_logfiles: " + ", ".join(missing))
                log("REFUSED: " + summary["reason"])
                return 3
            if not _take_lock(out_dir):
                summary.update(mode="locked", reason="another run holds retention/run.lock")
                log(summary["reason"])
                return 4
            lock_taken = True
            _below_normal()
            for name in os.listdir(status_dir):
                if name.endswith((".gz.tmp", ".restore.tmp")):
                    _rm(os.path.join(status_dir, name))
                    log(f"removed a leftover {name}")
        todo, held = scan(status_dir, archive_dir, now=now, cutoff_days=cutoff_days)
        classes = {}
        for r in held:
            k = _held_class(r["reason"])
            classes[k] = classes.get(k, 0) + 1
        summary.update(candidates=len(todo), candidates_gb=_gb(sum(r["size"] for r in todo)),
                       held=len(held), held_gb=_gb(sum(r["size"] for r in held)), held_reasons=classes)
        log(f"{mode}: {len(todo)} files ({summary['candidates_gb']:.2f} GB) older than {cutoff_days} days; "
            f"{len(held)} old files held ({summary['held_gb']:.2f} GB)"
            + (f"; {summary['reason']}" if summary["reason"] else ""))
        cap = max_gb * GB
        if mode == "dry-run":
            first = tot = 0
            for r in todo:
                if tot and tot + r["size"] > cap:
                    break
                tot += r["size"]
                first += 1
            summary.update(next_run=first, next_run_gb=_gb(tot))
            with open(os.path.join(out_dir, "dry_run.txt"), "w", encoding="ascii", errors="replace") as f:
                f.write(f"# {summary['ts']} dry run: {len(todo)} files ({summary['candidates_gb']:.2f} GB) "
                        f"would compress; an --apply run takes the first {first} ({tot / GB:.2f} GB, the "
                        f"{max_gb:g} GB cap); {len(held)} old files are held\n")
                for r in todo:
                    f.write(f"compress {r['name']} {r['size'] / 1e6:.1f} MB\n")
                for r in held:
                    f.write(f"hold {r['name']} {r['size'] / 1e6:.1f} MB: {r['reason']}\n")
            return 0
        done_in = out_bytes = freed = 0
        rc = 0
        for item in todo:
            if done_in and done_in + item["size"] > cap:
                summary["stopped"] = f"the {max_gb:g} GB cap"
                break
            if (time.time() - t0) / 60.0 > max_minutes:
                summary["stopped"] = f"the {max_minutes:g}-minute budget"
                break
            try:
                free = shutil.disk_usage(status_dir).free
            except OSError:
                free = None
            if free is not None and free < item["size"] * 0.35 + GB:
                summary["stopped"] = f"too little free disk to compress safely ({free / GB:.1f} GB)"
                break
            if held_open(item["path"]):
                summary["skipped"]["held open"] = summary["skipped"].get("held open", 0) + 1
                log(f"skipped {item['name']}: held open")
                continue
            try:
                res = compress_file(item["path"])
            except OSError as e:
                res = {"action": "failed", "reason": repr(e)}
            act = res["action"]
            if act in ("compressed", "removed_plain", "plain_held", "failed", "conflict"):
                _manifest(out_dir, item, res, mode)
            if act in ("compressed", "removed_plain", "plain_held"):
                summary[act] += 1
                gz_size = res.get("gz_size") or 0
                done_in += item["size"]
                if act == "compressed":            # the plain copy is gone, the .gz is new
                    out_bytes += gz_size
                    freed += item["size"] - gz_size
                elif act == "removed_plain":       # the .gz was already there
                    freed += item["size"]
                else:                              # plain_held: both stay until the next run
                    out_bytes += gz_size
                summary.update(in_gb=_gb(done_in), out_gb=_gb(out_bytes), freed_gb=_gb(freed),
                               in_bytes=done_in, freed_bytes=freed)
                log(f"{act} {item['name']} {item['size'] / 1e6:.1f} MB -> {gz_size / 1e6:.1f} MB")
            elif act == "skipped":
                summary["skipped"][res["reason"]] = summary["skipped"].get(res["reason"], 0) + 1
                log(f"skipped {item['name']}: {res['reason']}")
            elif act == "failed":
                summary["failed"].append({"file": item["name"], "reason": res.get("reason")})
                log(f"FAILED {item['name']}: {res.get('reason')}")
                rc = max(rc, 1)
            elif act == "conflict":
                summary["conflicts"].append({"file": item["name"], "reason": res.get("reason")})
                summary["stopped"] = "a .gz conflict"
                log(f"CONFLICT {item['name']}: {res.get('reason')}; stopping")
                rc = 2
                break
        left = [r for r in todo if os.path.exists(r["path"])]
        summary.update(remaining=len(left), remaining_gb=_gb(sum(r["size"] for r in left)))
        log(f"done: {summary['compressed']} compressed, {summary['in_gb']:.2f} GB in, "
            f"{summary['freed_gb']:.2f} GB freed, {len(summary['failed'])} failed, {len(left)} left"
            + (f"; stopped at {summary['stopped']}" if summary["stopped"] else ""))
        return rc
    finally:
        summary["elapsed_s"] = round(time.time() - t0, 1)
        try:
            summary["disk_free_gb"] = _gb(shutil.disk_usage(status_dir).free)
        except OSError:
            summary["disk_free_gb"] = None
        if summary.get("mode") != "locked":
            try:
                _write_json(os.path.join(out_dir, "last_run.json"), summary)
            except OSError as e:
                log(f"! last_run.json write failed: {e!r}")
        if lock_taken:
            _rm(os.path.join(out_dir, "run.lock"))
        if _LOG_FH is not None:
            _LOG_FH.close()
            _LOG_FH = None


def restore(paths, all_: bool = False, status_dir: str = None, out_dir: str = None) -> int:
    """Undo: the originals back from their .gz, checked against the manifest."""
    status_dir = status_dir or STATUS_DIR
    out_dir = out_dir or os.path.join(status_dir, "retention")
    os.makedirs(out_dir, exist_ok=True)
    index = {}
    try:
        with open(os.path.join(out_dir, "manifest.jsonl"), encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("action") in ("compressed", "removed_plain", "plain_held") and r.get("file"):
                    index[r["file"]] = r
    except OSError:
        pass
    if all_:
        paths = [os.path.join(status_dir, f + ".gz") for f in sorted(index)
                 if os.path.exists(os.path.join(status_dir, f + ".gz"))]
    rc = 0
    for gz in paths:
        if not gz.endswith(".gz") or not os.path.exists(gz):
            log(f"not a .gz on disk: {gz}")
            rc = 1
            continue
        try:
            res = restore_file(gz, index.get(os.path.basename(gz)[:-3]))
        except OSError as e:
            res = {"action": "failed", "reason": repr(e)}
        _manifest(out_dir, {"name": os.path.basename(gz)[:-3]}, res, "restore")
        log(f"{res['action']} {os.path.basename(gz)}" + (f": {res['reason']}" if res.get("reason") else ""))
        if res["action"] != "restored":
            rc = 1
    return rc


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["restore"]:
        ap = argparse.ArgumentParser(prog="imm_log_retention.py restore")
        ap.add_argument("paths", nargs="*")
        ap.add_argument("--all", action="store_true", help="every file in the manifest that is still .gz")
        ap.add_argument("--status-dir")
        a = ap.parse_args(argv[1:])
        if not a.paths and not a.all:
            ap.error("give PATH.gz ... or --all")
        return restore(a.paths, all_=a.all, status_dir=a.status_dir)
    ap = argparse.ArgumentParser(description="Compress the IMM's text logs older than the cutoff (dry run "
                                             "unless --apply).")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--not-before", help="YYYY-MM-DD (local): --apply stays a dry run before this date")
    ap.add_argument("--max-gb", type=float, default=10.0)
    ap.add_argument("--max-minutes", type=float, default=90.0)
    ap.add_argument("--cutoff-days", type=int, default=CUTOFF_DAYS,
                    help=f"only raises it: below {CUTOFF_DAYS} the dashboard still rewrites those days")
    ap.add_argument("--status-dir")
    ap.add_argument("--archive-dir")
    ap.add_argument("--out-dir", help="where last_run.json / dry_run.txt / manifest go (default STATUS_DIR/retention)")
    a = ap.parse_args(argv)
    if a.cutoff_days < CUTOFF_DAYS:
        ap.error(f"--cutoff-days must be at least {CUTOFF_DAYS} (the dashboard's history + {MARGIN_DAYS})")
    if a.not_before:
        date.fromisoformat(a.not_before)
    return run(apply=a.apply, status_dir=a.status_dir, archive_dir=a.archive_dir, out_dir=a.out_dir,
               max_gb=a.max_gb, max_minutes=a.max_minutes, not_before=a.not_before, cutoff_days=a.cutoff_days)


if __name__ == "__main__":
    sys.exit(main())
