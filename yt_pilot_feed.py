#!/usr/bin/env python3
"""Durable YouTube Data API feed for the KXYTVIEWSW pilot's fair (yt_weekly_fair).

WHY (2026-10-06): the pilot's fair reads hourly viewCount snapshots of every
tracked video of its artists. Until now those came only from
KL-data/youtube-collect/yt_artists.py, a bare research process (no Windows
task: a reboot kills it) started 9/30 with --until 2026-10-18T12:00Z. After
that the fair goes stale_api and the gate stands the family aside, while the
26OCT18 week trades to ~10/20. This is the pilot's own feed, run by the
"KL ytw-feed" scheduled task (register_ytw_feed.ps1 -> run_ytw_feed_hidden.vbs):
hourly at :00 and at logon, ONE pass, then exit.

A PASS
  scan      when SCAN_EVERY_H (3) h have passed since the last: the newest 50
            uploads of every tracked channel (playlistItems, 1 unit each; 22
            channels for KATSEYE / Drake / The Weeknd / Tate McRae, 13 with
            an uploads playlist); a video not yet tracked -- a release since
            setup -- joins its artist's set
  snapshot  videos.list part=statistics, 50 ids a call (1 unit), for every
            tracked video of yt_weekly_fair.ARTISTS (1,909 videos, 39 calls
            on 10/06); each batch stamped with its request's midpoint
Output: yt_weekly_fair.FEED_SNAP_FILE (yt_pilot_snapshots.jsonl), the
collector's line format {"ts", "artist", "id", "views"} in a file of its own
(two processes appending to one file can tear lines on Windows); the fair
reads both files and merges them per video.

VIDEO LISTS: IDS_FILE (yt_pilot_ids.json), seeded from the collector's
yt_artist_ids.json, which every pass re-reads to merge in what the collector
found -- never writing it (the collector rewrites it from memory). A new pilot
artist needs a row there first (yt_artists.py's setup finds the channels).

QUOTA (10,000 units/day per key, reset at midnight Pacific; the collectors
share this key): on the PT day of 10/05 they spent 4,952 (yt_artists 22
snapshots x 158 + 7 scans x 84; yt_collect 72 x 9 + 8 discovers x 30). This
feed: ~24 x 39 + 8 x 13 = ~1,040/day, so ~6,000 together while they run. Its own
ledger (every request counts, an error too) hard-caps it at DAILY_UNITS
(2,000); a pass the remaining budget cannot snapshot is skipped whole.
MIN_GAP_MIN stops a burst of runs (logon + :00, a manual run) from
double-snapshotting. A quotaExceeded 403 ends the pass (what it read is kept)
and blocks the feed to the next Pacific midnight. A pass makes no request
after PASS_DEADLINE_S (10 min; the task's limit is 15), keeping what it read.

KEY: ~/Downloads/youtube api key.txt, sent in the X-Goog-Api-Key header --
never in a URL, so never in an exception's text -- and never printed, logged
or written; every logged line is scrubbed of it besides.

Run: python yt_pilot_feed.py [--force] [--scan] [--dry-run] [--status].
Exit 0 ok / skipped, 1 a partial or failed pass, 2 no key or no video
list, 3 another pass holds the lock.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import yt_weekly_fair as ytw

try:
    from zoneinfo import ZoneInfo
    PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:                    # no tz database: PDT, near enough for a ledger day
    PACIFIC = timezone(timedelta(hours=-7))

API = "https://www.googleapis.com/youtube/v3"
KEY_FILE = os.path.join(os.path.expanduser("~"), "Downloads", "youtube api key.txt")
OUT_FILE = ytw.FEED_SNAP_FILE
DATA_DIR = os.path.dirname(OUT_FILE)
SOURCE_IDS_FILE = os.environ.get("IMM_YTW_FEED_SOURCE_IDS", os.path.join(DATA_DIR, "yt_artist_ids.json"))
IDS_FILE = os.path.join(DATA_DIR, "yt_pilot_ids.json")
STATE_FILE = os.path.join(DATA_DIR, "yt_pilot_feed_state.json")
LOG_FILE = os.path.join(DATA_DIR, "yt_pilot_feed.log")
LOCK_FILE = os.path.join(DATA_DIR, "yt_pilot_feed.lock")
DAILY_UNITS = int(ytw._env_float("IMM_YTW_FEED_DAILY_UNITS", 2000))
MIN_GAP_MIN = ytw._env_float("IMM_YTW_FEED_MIN_GAP_MIN", 40)
SCAN_EVERY_H = ytw._env_float("IMM_YTW_FEED_SCAN_EVERY_H", 3)
BATCH = 50
RETRIES = 3
PASS_DEADLINE_S = 600           # no new request after this; the task is killed at 15 min
_SECRETS: List[str] = []


def scrub(msg: str) -> str:
    for s in _SECRETS:
        msg = msg.replace(s, "<key>")
    return msg


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} {scrub(msg)}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None and sys.stdout.isatty():
        print(line.encode("ascii", "replace").decode(), flush=True)


def api_key(path: Optional[str] = None) -> str:
    with open(path or KEY_FILE, encoding="utf-8-sig") as f:
        raw = f.read()
    m = re.search(r"AIza[0-9A-Za-z_\-]{35}", raw)
    key = m.group(0) if m else raw.strip()
    if key:
        _SECRETS.append(key)
    return key


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def pt_day(ts: float) -> str:
    """The quota day (Pacific) holding ts."""
    return datetime.fromtimestamp(ts, PACIFIC).date().isoformat()


def next_pt_midnight(ts: float) -> float:
    d = datetime.fromtimestamp(ts, PACIFIC) + timedelta(days=1)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# the API

class QuotaExceeded(RuntimeError):
    """The key's daily quota is spent (403 quotaExceeded / dailyLimitExceeded)."""


class BudgetSpent(RuntimeError):
    """This feed's own DAILY_UNITS for the Pacific day are spent."""


class Deadline(RuntimeError):
    """The pass ran PASS_DEADLINE_S (a hung network): stop, keep what was read."""


class ApiError(RuntimeError):
    def __init__(self, msg: str, code: int = 0, reason: str = ""):
        super().__init__(msg)
        self.code, self.reason = code, reason


class Ledger:
    """Units this feed spent on the current Pacific day, kept in the state."""

    def __init__(self, state: dict, cap: int, now_ts: float):
        self.q = state.setdefault("quota", {})
        day = pt_day(now_ts)
        if self.q.get("day") != day:
            self.q.clear()
            self.q.update(day=day, units=0)
        self.cap = cap

    @property
    def units(self) -> int:
        return self.q["units"]

    @property
    def remaining(self) -> int:
        return self.cap - self.q["units"]

    def charge(self, n: int = 1) -> None:
        if self.q["units"] + n > self.cap:
            raise BudgetSpent(f"{self.q['units']}/{self.cap} units spent on PT {self.q['day']}")
        self.q["units"] += n


def _http_get(url: str, headers: Dict[str, str], timeout: float = 30) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _error_reason(body: bytes) -> Tuple[str, str]:
    try:
        err = json.loads(body).get("error") or {}
        return str(((err.get("errors") or [{}])[0]).get("reason", "")), str(err.get("message", ""))
    except (ValueError, AttributeError, IndexError):
        return "", ""


_RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}


class YouTube:
    """GETs against the Data API: the key in a header, every attempt charged
    to the ledger, transient failures retried."""

    def __init__(self, key: str, ledger: Ledger, fetch: Optional[Callable] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.key, self.ledger = key, ledger
        if key and key not in _SECRETS:
            _SECRETS.append(key)
        self.fetch = fetch or _http_get
        self.sleep = sleep
        self.calls = 0
        self.errors: List[str] = []
        self.t_end = time.monotonic() + PASS_DEADLINE_S

    def get(self, path: str, **params) -> dict:
        url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
        headers = {"X-Goog-Api-Key": self.key, "Accept": "application/json"}
        last = ""
        for i in range(RETRIES):
            if time.monotonic() > self.t_end:
                raise Deadline(f"{PASS_DEADLINE_S} s")
            self.ledger.charge(1)
            self.calls += 1
            try:
                code, body = self.fetch(url, headers)
            except (OSError, http.client.HTTPException) as e:   # URLError, timeouts, resets
                last = scrub(f"{type(e).__name__}: {e}")       # scrubbed before any cut
                self.sleep(2 + 4 * i)
                continue
            if code == 200:
                return json.loads(body)
            reason, msg = _error_reason(body)
            if code == 403 and reason in ("quotaExceeded", "dailyLimitExceeded"):
                raise QuotaExceeded(reason)
            if code in (429, 500, 502, 503, 504) or reason in _RETRY_REASONS:
                last = f"HTTP {code} {reason}".strip()
                self.sleep(2 + 4 * i)
                continue
            raise ApiError(f"{path} HTTP {code} {reason}: {scrub(msg)[:120]}", code, reason)
        raise ApiError(f"{path} failed after {RETRIES} tries: {last[:160]}")


# ---------------------------------------------------------------------------
# video lists

def load_ids(names: List[str], save: bool = True) -> Tuple[Dict[str, dict], List[str]]:
    """The feed's own {artist: {"channels": [{"id", "title"}], "videos": [id]}}
    for `names`, topped up from the collector's file (read only; a file caught
    mid-rewrite is skipped this pass). Returns it and what was merged."""
    own = _read_json(IDS_FILE) or {}
    src = _read_json(SOURCE_IDS_FILE) or {}
    notes, changed = [], False
    for n in names:
        s = src.get(n) if isinstance(src, dict) else None
        rec = own.get(n)
        if rec is None:
            if not s:
                continue
            rec = own[n] = {"channels": [], "videos": []}
        if not s:
            continue
        have_c = {c["id"] for c in rec["channels"]}
        for c in s.get("channels") or []:
            if c.get("id") and c["id"] not in have_c:
                rec["channels"].append({"id": c["id"], "title": c.get("title", "")})
                have_c.add(c["id"])
                changed = True
        have = set(rec["videos"])
        new = []
        for v in s.get("videos") or []:
            if v and v not in have:
                new.append(v)
                have.add(v)
        if new:
            rec["videos"] += new
            changed = True
            notes.append(f"{n}: +{len(new)} videos from {os.path.basename(SOURCE_IDS_FILE)}")
    if changed and save:
        _write_json(IDS_FILE, own)
    return own, notes


def uploads_playlist(channel_id: str) -> Optional[str]:
    """A channel's uploads playlist: 'UC...' -> 'UU...'."""
    return "UU" + channel_id[2:] if channel_id.startswith("UC") else None


NO_UPLOADS_RECHECK_D = 7


def scan_uploads(yt: YouTube, ids: Dict[str, dict], names: List[str],
                 now_ts: float) -> List[Tuple[str, str]]:
    """The newest 50 uploads of every tracked channel; untracked ones join
    their artist's list (in `ids`). A channel without an uploads playlist (9
    of the pilot's Topic channels on 10/06) is marked and skipped for
    NO_UPLOADS_RECHECK_D days. Returns [(artist, video)] added."""
    added = []
    for n in names:
        rec = ids.get(n)
        if not rec:
            continue
        have = set(rec["videos"])
        for ch in rec["channels"]:
            up = uploads_playlist(ch["id"])
            if not up or now_ts - float(ch.get("no_uploads_at") or 0) < NO_UPLOADS_RECHECK_D * 86400:
                continue
            try:
                d = yt.get("playlistItems", part="contentDetails", playlistId=up, maxResults=50)
            except ApiError as e:
                if e.reason == "playlistNotFound":
                    ch["no_uploads_at"] = now_ts
                    log(f"scan: {n} channel {ch['id']} ({ch.get('title', '')}) has no uploads "
                        f"playlist; skipped for {NO_UPLOADS_RECHECK_D} days")
                else:
                    yt.errors.append(f"scan {n} {ch['id']}: {e}")
                continue
            for it in d.get("items") or []:
                v = (it.get("contentDetails") or {}).get("videoId")
                if v and v not in have:
                    rec["videos"].append(v)
                    have.add(v)
                    added.append((n, v))
    return added


def snapshot(yt: YouTube, ids: Dict[str, dict], names: List[str], lines: List[str],
             counts: Dict[str, int], clock: Callable[[], float] = time.time) -> None:
    """Every tracked video's viewCount, appended to `lines` as jsonl and
    counted per artist in `counts` (so a pass cut short keeps what it read)."""
    for n in names:
        vids = sorted(set((ids.get(n) or {}).get("videos") or []))
        for i in range(0, len(vids), BATCH):
            t0 = clock()
            try:
                d = yt.get("videos", part="statistics", id=",".join(vids[i:i + BATCH]))
            except ApiError as e:
                yt.errors.append(f"snapshot {n} batch {i // BATCH}: {e}")
                continue
            ts = (t0 + clock()) / 2
            for it in d.get("items") or []:
                vc = (it.get("statistics") or {}).get("viewCount")
                if vc is None:                 # hidden count: no line beats a 0
                    continue
                lines.append(json.dumps({"ts": ts, "artist": n, "id": it["id"],
                                         "views": int(vc)}) + "\n")
                counts[n] = counts.get(n, 0) + 1


# ---------------------------------------------------------------------------
# a pass

def run_pass(now_ts: Optional[float] = None, force: bool = False, force_scan: bool = False,
             fetch: Optional[Callable] = None, key: Optional[str] = None,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.time) -> Tuple[int, str]:
    """One pass (scan when due, then the snapshot). Returns (exit code, summary)."""
    now_ts = now_ts if now_ts is not None else clock()
    state = _read_json(STATE_FILE) or {}
    names = list(ytw.ARTISTS.values())
    blocked = float(state.get("blocked_until") or 0)
    if now_ts < blocked:
        msg = f"skip: the key's daily quota ran out; blocked to {_iso(blocked)}"
        log(msg)
        return 0, msg
    last = float(state.get("last_snapshot") or 0)
    if not force and now_ts - last < MIN_GAP_MIN * 60:
        msg = f"skip: last snapshot {(now_ts - last) / 60:.0f} min ago (< {MIN_GAP_MIN:g})"
        log(msg)
        return 0, msg
    ids, notes = load_ids(names)
    for x in notes:
        log(x)
    n_vids = {n: len(set((ids.get(n) or {}).get("videos") or [])) for n in names}
    need = sum(math.ceil(v / BATCH) for v in n_vids.values())
    for n in names:
        if not n_vids[n]:
            log(f"! no tracked videos for {n} (add it to {os.path.basename(SOURCE_IDS_FILE)})")
    if need == 0:
        return 2, "no video list"
    ledger = Ledger(state, DAILY_UNITS, now_ts)
    if ledger.remaining < need:
        msg = (f"skip: {ledger.remaining} units left of this feed's {DAILY_UNITS} on PT "
               f"{ledger.q['day']}, the snapshot needs {need}")
        log(msg)
        _write_json(STATE_FILE, state)
        return 1, msg
    try:
        key = key or api_key()
    except OSError as e:
        msg = f"! no API key ({type(e).__name__} reading {KEY_FILE})"
        log(msg)
        return 2, msg
    yt = YouTube(key, ledger, fetch=fetch, sleep=sleep)
    lines: List[str] = []
    added: List[Tuple[str, str]] = []
    counts: Dict[str, int] = {}
    scan_calls, status = None, "ok"
    try:
        n_ch = sum(1 for n in names for c in (ids.get(n) or {}).get("channels") or []
                   if now_ts - float(c.get("no_uploads_at") or 0) >= NO_UPLOADS_RECHECK_D * 86400)
        due = force_scan or now_ts - float(state.get("last_scan") or 0) >= SCAN_EVERY_H * 3600 - 600
        if due and ledger.remaining >= need + n_ch:     # the snapshot comes first
            c0 = yt.calls
            added = scan_uploads(yt, ids, names, now_ts)
            scan_calls = yt.calls - c0
            state["last_scan"] = now_ts
            for n, v in added:
                log(f"new upload tracked: {n} {v}")
            _write_json(IDS_FILE, ids)            # new videos, channel marks
        snapshot(yt, ids, names, lines, counts, clock)
    except QuotaExceeded as e:
        status = f"quota ({e})"
        state["blocked_until"] = next_pt_midnight(now_ts)
    except BudgetSpent as e:
        status = f"budget ({e})"
    except Deadline as e:
        status = f"deadline ({e})"
    finally:
        if lines:
            with open(OUT_FILE, "a", encoding="utf-8", newline="\n") as f:
                f.write("".join(lines))
            state["last_snapshot"] = now_ts
        expect = sum(n_vids.values())
        if status == "ok" and (yt.errors or len(lines) < 0.95 * expect):
            status = "partial"
        state["last_pass"] = {"at": _iso(now_ts), "status": status, "videos": len(lines),
                              "expected": expect, "calls": yt.calls, "scan_calls": scan_calls,
                              "added": len(added), "errors": yt.errors[:5]}
        _write_json(STATE_FILE, state)
    for e in yt.errors[:5]:
        log(f"! {e}")
    per = ", ".join(f"{n} {counts.get(n, 0)}/{n_vids[n]}" for n in names)
    summary = (f"snapshot {len(lines)} videos ({per})"
               + ("" if scan_calls is None else f" | scan {scan_calls} calls, +{len(added)} new")
               + f" | {yt.calls} calls, {ledger.units}/{DAILY_UNITS} units PT {ledger.q['day']}"
               + ("" if status == "ok" else f" | {status}"))
    log(summary)
    if status.startswith("quota"):
        log(f"! the key's daily quota ran out: blocked to {_iso(state['blocked_until'])}")
    return (0 if status == "ok" else 1), summary


def single_instance():
    """An exclusive lock on LOCK_FILE for this process's life; None if held."""
    f = open(LOCK_FILE, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def status_text(now_ts: Optional[float] = None) -> str:
    now_ts = now_ts if now_ts is not None else time.time()
    state = _read_json(STATE_FILE) or {}
    last = float(state.get("last_snapshot") or 0)
    q = state.get("quota") or {}
    try:
        size = os.path.getsize(OUT_FILE)
    except OSError:
        size = 0
    out = [f"artists {', '.join(ytw.ARTISTS.values())}",
           f"last snapshot {_iso(last) if last else 'never'}"
           + (f" ({(now_ts - last) / 60:.0f} min ago)" if last else ""),
           f"last scan {_iso(float(state['last_scan'])) if state.get('last_scan') else 'never'}",
           f"units PT {q.get('day', '-')}: {q.get('units', 0)}/{DAILY_UNITS}",
           f"last pass {json.dumps(state.get('last_pass'))}",
           f"{OUT_FILE}: {size / 1e6:.1f} MB"]
    if float(state.get("blocked_until") or 0) > now_ts:
        out.append(f"BLOCKED (key quota) to {_iso(float(state['blocked_until']))}")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="One pass of the KXYTVIEWSW pilot's YouTube API feed.")
    ap.add_argument("--force", action="store_true", help=f"ignore the {MIN_GAP_MIN:g}-min gap guard")
    ap.add_argument("--scan", action="store_true", help="run the new-upload scan this pass")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only: no key read, no API call, nothing written")
    ap.add_argument("--status", action="store_true", help="print the feed's state and exit")
    a = ap.parse_args(argv)
    if a.status:
        print(status_text())
        return 0
    if a.dry_run:
        names = list(ytw.ARTISTS.values())
        ids, notes = load_ids(names, save=False)
        state = _read_json(STATE_FILE) or {}
        for n in names:
            rec = ids.get(n) or {}
            nv = len(set(rec.get("videos") or []))
            print(f"{n}: {nv} videos -> {math.ceil(nv / BATCH)} videos.list calls; "
                  f"{len(rec.get('channels') or [])} channels to scan")
        for x in notes:
            print("would merge:", x)
        print(f"scan due: {time.time() - float(state.get('last_scan') or 0) >= SCAN_EVERY_H * 3600 - 600}; "
              f"budget {DAILY_UNITS}/PT day; out {OUT_FILE}")
        return 0
    lock = single_instance()
    if lock is None:
        log("another pass holds the lock; exiting")
        return 3
    try:
        code, _ = run_pass(force=a.force, force_scan=a.scan)
        return code
    except Exception as e:
        log(f"! pass failed: {type(e).__name__}: {scrub(str(e))[:200]}")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
