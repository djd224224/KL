#!/usr/bin/env python3
"""Durable YouTube Data API feed for the global top-video markets: the
candidates of KXYTDAILYTOPVIDEOG (#1) and KXYTTOPVIDEOG2D (#2), and the music
videos most likely to join them, snapshot every 20 minutes -- the data a
view-count fair for the IMM's #2 pilot would read.

WHY (Jack 2026-10-14: "set up the feed and run the preview"). The #2 pilot
quotes with no fair, and its fills lose to traders who watch live view counts.
The only record of the candidates' views is KL-data/youtube-collect/
yt_collect.py, a bare research process (no task: a reboot kills it) whose
--until is 2026-10-18 12:00Z. It also stopped adding videos at its 450 cap,
so a release since then goes untracked.

A PASS (the "KL yt-topvid-feed" task, register_yt_topvid_feed.ps1 ->
run_yt_topvid_feed_hidden.vbs: every 20 min and at logon; one pass, exit):
  candidates  the open #1 / #2 events' markets (one signed Kalshi list read
              per series, kalshi_reads), each matched to its video: an id in
              the market name ("KALYANI (Remix) [YouTube video 12:
              z5y8Clp_TdE]"), else the feed's own name map, else the most
              viewed tracked video whose title + channel hold every word of
              one of the name's " / " alternatives (match_video), else one
              search.list (SEARCH_UNITS; SEARCH_PER_DAY a Pacific day, one try
              a day per name). Kept in IDS_FILE per event, event -> market
              code -> video: the join a fair and its backtests need.
  discover    every DISCOVER_EVERY_H (3) h: each region's mostPopular music
              videos (videoCategoryId 10, 1 unit a region), so a new release
              is tracked from its first day (the collector's discover)
  snapshot    videos.list, 50 ids a call (1 unit), for every tracked video,
              each batch stamped with its request's midpoint; the call also
              refreshes the titles and channels the matching reads
The snapshot runs first, so nothing after it can cost it, and the candidates
are matched on its fresh view counts; what the steps after it add (a
candidate's own video, discoveries, search hits) is snapshot from the next
pass, 20 minutes on.
TRACKED: every video a candidate or on a mostPopular list in the last
KEEP_DAYS, seeded once from the collector's yt_ids.json (read only, never
written). Past MAX_TRACKED the slowest go first, by views a day from the
snapshots (note_views); a video added in the last GRACE_SECS stays until it
has a rate, and the open candidates always stay. (The first live pass, with
a plain longest-unseen rule, dropped the collector's contenders for regional
mostPopular filler: they had all been seen that minute.)
Output: OUT_FILE (yt_topvid_snapshots.jsonl), the collector's line format
{"ts", "id", "views"} in a file of its own (two processes appending to one
file can tear lines on Windows).

QUOTA (10,000 units/day per key, reset at midnight Pacific, shared with the
collectors and yt_pilot_feed): ~72 x 12 + 8 x 30 = ~1,100/day, searches
aside; this feed's own ledger stops it at DAILY_UNITS (2,000), the snapshot
first. The API layer is yt_pilot_feed's: the key in the X-Goog-Api-Key
header only and scrubbed from every logged line, every attempt charged,
transient errors retried, a PASS_DEADLINE_S per pass, and a quotaExceeded
403 blocks the feed to the next Pacific midnight.

Run: python yt_topvid_feed.py [--force] [--discover] [--dry-run] [--status].
Exit 0 ok / skipped, 1 a partial or failed pass (the Kalshi read included),
2 no key, 3 another pass holds the lock.
"""
from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import yt_pilot_feed as ypf

DATA_DIR = os.path.dirname(ypf.OUT_FILE)
OUT_FILE = os.path.join(DATA_DIR, "yt_topvid_snapshots.jsonl")
IDS_FILE = os.path.join(DATA_DIR, "yt_topvid_ids.json")
SOURCE_IDS_FILE = os.environ.get("IMM_YTV_FEED_SOURCE_IDS", os.path.join(DATA_DIR, "yt_ids.json"))
STATE_FILE = os.path.join(DATA_DIR, "yt_topvid_feed_state.json")
LOG_FILE = os.path.join(DATA_DIR, "yt_topvid_feed.log")
LOCK_FILE = os.path.join(DATA_DIR, "yt_topvid_feed.lock")
SERIES = ("KXYTDAILYTOPVIDEOG", "KXYTTOPVIDEOG2D")
DAILY_UNITS = int(ypf.ytw._env_float("IMM_YTV_FEED_DAILY_UNITS", 2000))
MIN_GAP_MIN = ypf.ytw._env_float("IMM_YTV_FEED_MIN_GAP_MIN", 15)
DISCOVER_EVERY_H = ypf.ytw._env_float("IMM_YTV_FEED_DISCOVER_EVERY_H", 3)
SEARCH_PER_DAY = int(ypf.ytw._env_float("IMM_YTV_FEED_SEARCH_PER_DAY", 5))
SEARCH_UNITS = 100               # search.list's quota cost
KEEP_DAYS = 14
# past MAX_TRACKED, the slowest (views a day, RATE_MIN_SECS+ of history) go
# first; a video added in the last GRACE_SECS stays meanwhile (no rate yet)
MAX_TRACKED = 600
GRACE_SECS = 2 * 3600
RATE_MIN_SECS = 3600
RATE_REF_SECS = 6 * 3600         # the rate's base snapshot moves on this often
REDISCOVER_DAYS = 3              # a video the cap dropped is not re-discovered sooner
BATCH = ypf.BATCH
# the collector's regions (yt_collect.discover) less UZ, which mostPopular
# refuses (HTTP 400 invalidRegionCode)
REGIONS = ("US,IN,BR,MX,ID,PH,KR,JP,GB,DE,TR,TH,VN,EG,SA,NG,PK,CO,AR,ES,"
           "FR,IT,RU,BD,MY,ZA,KE,CA,AU").split(",")
SNAPSHOT_FIELDS = "items(id,snippet(title,channelTitle),statistics(viewCount))"


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} {ypf.scrub(msg)}"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if sys.stdout is not None and sys.stdout.isatty():
        print(line.encode("ascii", "replace").decode(), flush=True)


# ---------------------------------------------------------------------------
# which video a market means

_ID_IN_NAME = re.compile(r"YouTube video \d+:\s*([A-Za-z0-9_-]{11})")
_WORD = re.compile(r"[^\W_]+")
# words a market name or a video title carries that say nothing about which
# video it is
_NOISE = frozenset("official video music audio lyric lyrics mv hd 4k full song "
                   "klip clip visualizer".split())


def words(s: str) -> set:
    return {w for w in _WORD.findall((s or "").lower()) if w not in _NOISE}


_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def alternatives(name: str) -> List[set]:
    """The word sets a market name can be matched on: each " / " part, and
    each part without its (...) / [...] tags ('Radhimaa [From "Think
    Indie"]' -> also {radhimaa})."""
    out: List[set] = []
    for part in re.split(r"\s+/\s+", name or ""):
        for text in (part, _BRACKETED.sub(" ", part)):
            w = words(text)
            if w and w not in out:
                out.append(w)
    return out


def match_video(name: str, videos: Dict[str, dict]) -> Optional[str]:
    """The video a market name means, or None: an id written in the name;
    else, among `videos` ({id: {"title", "channel", "views"}}), the one whose
    title + channel hold every word of one of the name's alternatives (noise
    words aside), the most viewed first, then the closest title."""
    m = _ID_IN_NAME.search(name or "")
    if m:
        return m.group(1)
    alts = alternatives(name)
    best, best_key = None, None
    for vid, v in videos.items():
        have = words(v.get("title", "")) | words(v.get("channel", ""))
        if not any(a <= have for a in alts):
            continue
        key = (int(v.get("views") or 0),
               difflib.SequenceMatcher(None, name.lower(), (v.get("title") or "").lower()).ratio())
        if best_key is None or key > best_key:
            best, best_key = vid, key
    return best


def kalshi_open_markets(series=SERIES) -> List[dict]:
    """The open markets of `series`: one signed list read per series."""
    from kalshi_reads import kalshi_get
    out: List[dict] = []
    for s in series:
        out += kalshi_get("/markets", {"series_ticker": s, "status": "open",
                                       "limit": 200}).get("markets") or []
    return out


def blank_ids() -> dict:
    return {"videos": {}, "events": {}, "names": {}, "search_tried": {}, "dropped": {}}


def load_ids() -> dict:
    ids = ypf._read_json(IDS_FILE)
    if not isinstance(ids, dict) or not isinstance(ids.get("videos"), dict):
        ids = blank_ids()
    for k, v in blank_ids().items():
        ids.setdefault(k, v)
    return ids


def seed_from_collector(ids: dict, now_ts: float) -> int:
    """Once: every video the collector tracks (its yt_ids.json, read only),
    so the two files cover the same videos while both run."""
    if ids.get("seeded_from_collector"):
        return 0
    src = ypf._read_json(SOURCE_IDS_FILE)
    if not isinstance(src, dict):
        return 0                       # missing or caught mid-rewrite: next pass
    n = 0
    for rec in src.values():
        vid = (rec or {}).get("id") if isinstance(rec, dict) else None
        if vid and vid not in ids["videos"]:
            ids["videos"][vid] = {"title": rec.get("yt_title", ""), "channel": rec.get("channel", ""),
                                  "src": "collector", "first": now_ts, "last": now_ts}
            n += 1
    ids["seeded_from_collector"] = now_ts
    return n


def map_candidates(ids: dict, markets: List[dict], now_ts: float) -> Tuple[set, List[Tuple[str, str, str]]]:
    """Each open market's video (see the module doc), recorded per event and
    marked seen now. Returns (the candidates' video ids, [(event, code, name)]
    still unmatched)."""
    cands, unmatched = set(), []
    for m in markets:
        t = str(m.get("ticker") or "")
        name = str(m.get("yes_sub_title") or m.get("subtitle") or "").strip()
        if "-" not in t or not name:
            continue
        ev = str(m.get("event_ticker") or t.rsplit("-", 1)[0])
        code = t.rsplit("-", 1)[1]
        evrec = ids["events"].setdefault(ev, {})
        rec = evrec.get(code) or {}
        vid = rec.get("id")
        if not vid:
            vid = ids["names"].get(name.lower())
            via = "name map"
            if not vid:
                vid = match_video(name, ids["videos"])
                via = "id in name" if _ID_IN_NAME.search(name) else "tracked title"
            if not vid:
                evrec[code] = {"name": name, "id": None}
                unmatched.append((ev, code, name))
                continue
            evrec[code] = {"name": name, "id": vid, "via": via, "at": now_ts}
            ids["names"][name.lower()] = vid
            v = ids["videos"].get(vid) or {}
            log(f"candidate {ev} {code} '{name}' -> {vid} ({via}: "
                f"'{(v.get('title') or '?')[:50]}' / {(v.get('channel') or '?')[:30]})")
        v = ids["videos"].setdefault(vid, {"title": "", "channel": "", "src": f"kalshi {ev}",
                                           "first": now_ts})
        v["last"] = now_ts
        cands.add(vid)
    return cands, unmatched


class _Cost:
    """A ledger view that charges `units` per API attempt (search.list costs
    100; yt_pilot_feed.YouTube charges 1 per attempt)."""

    def __init__(self, ledger: ypf.Ledger, units: int):
        self.ledger, self.units = ledger, units

    def charge(self, n: int = 1) -> None:
        self.ledger.charge(n * self.units)


def search_unmatched(yts: ypf.YouTube, yt: ypf.YouTube, ids: dict,
                     unmatched: List[Tuple[str, str, str]], now_ts: float,
                     budget: Callable[[], bool]) -> int:
    """One search.list per unmatched name (each name at most once a day),
    while budget() allows; a result counts only if match_video accepts it,
    most viewed first -- the hits' counts come from one videos.list (1 unit)
    on `yt`: without them 'TRALALA' went to a reaction video (10/14). Returns
    how many markets it matched."""
    found = 0
    for ev, code, name in unmatched:
        if now_ts - float(ids["search_tried"].get(name.lower()) or 0) < 86400:
            continue
        if not budget():
            break
        ids["search_tried"][name.lower()] = now_ts
        try:
            d = yts.get("search", part="snippet", q=name, type="video", maxResults=5)
        except ypf.BudgetSpent:
            break                            # a retry ran past the day's cap
        except ypf.ApiError as e:
            yts.errors.append(f"search '{name}': {e}")
            continue
        hits = {}
        for it in d.get("items") or []:
            vid = (it.get("id") or {}).get("videoId")
            sn = it.get("snippet") or {}
            if vid:
                hits[vid] = {"title": sn.get("title", ""), "channel": sn.get("channelTitle", "")}
        if hits:
            try:
                st = yt.get("videos", part="statistics", id=",".join(hits),
                            fields="items(id,statistics(viewCount))")
            except ypf.BudgetSpent:
                break
            except ypf.ApiError as e:
                yt.errors.append(f"search views '{name}': {e}")
                st = {}
            for it in st.get("items") or []:
                vc = (it.get("statistics") or {}).get("viewCount")
                if it.get("id") in hits and vc is not None:
                    hits[it["id"]]["views"] = int(vc)
        vid = match_video(name, hits)
        if not vid:
            log(f"candidate {ev} {code} '{name}': no search result holds its words "
                f"({'; '.join(h['title'][:40] for h in hits.values()) or 'none'})")
            continue
        ids["videos"].setdefault(vid, {**hits[vid], "src": f"search {ev}", "first": now_ts})["last"] = now_ts
        ids["names"][name.lower()] = vid
        # every event's market of that name (#1 and #2 list the same videos)
        for ev2, code2, name2 in unmatched:
            if name2.lower() == name.lower():
                ids["events"].setdefault(ev2, {})[code2] = {"name": name2, "id": vid,
                                                            "via": "search", "at": now_ts}
                found += 1
        log(f"candidate {ev} {code} '{name}' -> {vid} (search: '{hits[vid]['title'][:50]}')")
    return found


def discover(yt: ypf.YouTube, ids: dict, now_ts: float) -> int:
    """Each region's mostPopular music videos: new ones join (not one the cap
    dropped in the last REDISCOVER_DAYS: it would be snapshot through another
    grace period only to be dropped again), known ones are marked seen.
    Returns how many joined."""
    added = 0
    dropped = ids.get("dropped") or {}
    for reg in REGIONS:
        try:
            d = yt.get("videos", part="snippet", chart="mostPopular", videoCategoryId="10",
                       regionCode=reg, maxResults=50, fields="items(id,snippet(title,channelTitle))")
        except ypf.ApiError as e:
            yt.errors.append(f"discover {reg}: {e}")
            continue
        for it in d.get("items") or []:
            sn = it.get("snippet") or {}
            v = ids["videos"].get(it.get("id"))
            if v is None and it.get("id") \
                    and now_ts - float(dropped.get(it["id"]) or 0) >= REDISCOVER_DAYS * 86400:
                ids["videos"][it["id"]] = {"title": sn.get("title", ""),
                                           "channel": sn.get("channelTitle", ""),
                                           "src": f"mostPopular {reg}", "first": now_ts, "last": now_ts}
                added += 1
            elif v is not None:
                v["last"] = now_ts
    return added


def note_views(rec: dict, ts: float, views: int) -> None:
    """A snapshot's count: rec["rate"] (views a day) against a base snapshot
    RATE_MIN_SECS+ older, the base moved every RATE_REF_SECS."""
    ref = rec.get("ref")
    if ref and ts - float(ref[0]) >= RATE_MIN_SECS:
        rec["rate"] = round((views - int(ref[1])) * 86400 / (ts - float(ref[0])))
    if not ref or ts - float(ref[0]) >= RATE_REF_SECS:
        rec["ref"] = [round(ts), views]
    rec["views"] = views


def prune(ids: dict, now_ts: float, keep: set) -> int:
    """Drop videos unseen for KEEP_DAYS; then, past MAX_TRACKED, the slowest
    (views a day; none known = 0) of those older than GRACE_SECS -- a new
    video stays until it has a rate, so the cap can be over for a while.
    `keep` (the open candidates) always stays. Returns the number dropped."""
    vids = ids["videos"]
    drop = [v for v, r in vids.items()
            if v not in keep and now_ts - float(r.get("last") or 0) > KEEP_DAYS * 86400]
    for v in drop:
        del vids[v]
    over = len(vids) - MAX_TRACKED
    remembered = ids.setdefault("dropped", {})
    for v in [v for v, t in remembered.items() if now_ts - float(t) >= REDISCOVER_DAYS * 86400]:
        del remembered[v]
    if over > 0:
        old = [v for v in vids if v not in keep
               and now_ts - float(vids[v].get("first") or 0) >= GRACE_SECS]
        old.sort(key=lambda v: (float(vids[v].get("rate") or 0), float(vids[v].get("last") or 0)))
        for v in old[:over]:
            del vids[v]
            remembered[v] = round(now_ts)
        drop += old[:over]
    return len(drop)


def snapshot(yt: ypf.YouTube, ids: dict, lines: List[str],
             clock: Callable[[], float] = time.time) -> None:
    """Every tracked video's viewCount, appended to `lines` as jsonl (a pass
    cut short keeps what it read); titles and channels refreshed in `ids`."""
    vids = sorted(ids["videos"])
    for i in range(0, len(vids), BATCH):
        t0 = clock()
        try:
            d = yt.get("videos", part="snippet,statistics", id=",".join(vids[i:i + BATCH]),
                       fields=SNAPSHOT_FIELDS)
        except ypf.ApiError as e:
            yt.errors.append(f"snapshot batch {i // BATCH}: {e}")
            continue
        ts = (t0 + clock()) / 2
        for it in d.get("items") or []:
            rec = ids["videos"].get(it.get("id"))
            sn = it.get("snippet") or {}
            if rec is not None:
                rec["title"] = sn.get("title") or rec.get("title", "")
                rec["channel"] = sn.get("channelTitle") or rec.get("channel", "")
            vc = (it.get("statistics") or {}).get("viewCount")
            if vc is None:                 # hidden count: no line beats a 0
                continue
            if rec is not None:
                note_views(rec, ts, int(vc))
            lines.append(json.dumps({"ts": ts, "id": it["id"], "views": int(vc)}) + "\n")


# ---------------------------------------------------------------------------
# a pass

def run_pass(now_ts: Optional[float] = None, force: bool = False, force_discover: bool = False,
             fetch: Optional[Callable] = None, key: Optional[str] = None,
             kalshi: Callable[[], List[dict]] = kalshi_open_markets,
             sleep: Callable[[float], None] = time.sleep,
             clock: Callable[[], float] = time.time) -> Tuple[int, str]:
    """One pass: the snapshot, then the candidates (matched on its fresh
    view counts), discovery when due, the searches. Returns (exit code,
    summary)."""
    now_ts = now_ts if now_ts is not None else clock()
    state = ypf._read_json(STATE_FILE) or {}
    blocked = float(state.get("blocked_until") or 0)
    if now_ts < blocked:
        msg = f"skip: the key's daily quota ran out; blocked to {ypf._iso(blocked)}"
        log(msg)
        return 0, msg
    last = float(state.get("last_snapshot") or 0)
    if not force and now_ts - last < MIN_GAP_MIN * 60:
        msg = f"skip: last snapshot {(now_ts - last) / 60:.0f} min ago (< {MIN_GAP_MIN:g})"
        log(msg)
        return 0, msg
    ids = load_ids()
    seeded = seed_from_collector(ids, now_ts)
    if seeded:
        log(f"seeded {seeded} videos from {os.path.basename(SOURCE_IDS_FILE)}")
    expect = len(ids["videos"])
    need = math.ceil(expect / BATCH)
    ledger = ypf.Ledger(state, DAILY_UNITS, now_ts)
    if need and ledger.remaining < need:
        msg = (f"skip: {ledger.remaining} units left of this feed's {DAILY_UNITS} on PT "
               f"{ledger.q['day']}, the snapshot needs {need}")
        log(msg)
        ypf._write_json(STATE_FILE, state)
        return 1, msg
    try:
        key = key or ypf.api_key()
    except OSError as e:
        msg = f"! no API key ({type(e).__name__} reading {ypf.KEY_FILE})"
        log(msg)
        return 2, msg
    yt = ypf.YouTube(key, ledger, fetch=fetch, sleep=sleep)
    yts = ypf.YouTube(key, _Cost(ledger, SEARCH_UNITS), fetch=fetch, sleep=sleep)
    lines: List[str] = []
    cands, unmatched = set(), []
    added = found = dropped = 0
    disc_calls, status, kalshi_note = None, "ok", ""
    searches = state.setdefault("searches", {})
    if searches.get("day") != ledger.q["day"]:
        searches.clear()
        searches.update(day=ledger.q["day"], n=0)

    def search_budget() -> bool:
        ok = searches["n"] < SEARCH_PER_DAY and ledger.remaining >= SEARCH_UNITS
        if ok:
            searches["n"] += 1
        return ok

    try:
        # THE SNAPSHOT FIRST: nothing after it can cost it, and the matching
        # below reads its view counts ("most viewed first"). What the steps
        # after it add is snapshot from the next pass.
        snapshot(yt, ids, lines, clock)
        try:
            cands, unmatched = map_candidates(ids, kalshi(), now_ts)
        except Exception as e:             # Kalshi down: the snapshot is kept
            kalshi_note = f" | Kalshi read failed ({type(e).__name__})"
            log(f"! Kalshi read failed: {type(e).__name__}: {ypf.scrub(str(e))[:160]}")
        due = force_discover or now_ts - float(state.get("last_discover") or 0) >= DISCOVER_EVERY_H * 3600 - 600
        if due and ledger.remaining >= len(REGIONS):
            c0 = yt.calls
            added = discover(yt, ids, now_ts)
            disc_calls = yt.calls - c0
            state["last_discover"] = now_ts
        if unmatched:
            found = search_unmatched(yts, yt, ids, unmatched, now_ts, search_budget)
        dropped = prune(ids, now_ts, cands)
    except ypf.QuotaExceeded as e:
        status = f"quota ({e})"
        state["blocked_until"] = ypf.next_pt_midnight(now_ts)
    except ypf.BudgetSpent as e:
        status = f"budget ({e})"
    except ypf.Deadline as e:
        status = f"deadline ({e})"
    finally:
        if lines:
            with open(OUT_FILE, "a", encoding="utf-8", newline="\n") as f:
                f.write("".join(lines))
            state["last_snapshot"] = now_ts
        errors = yt.errors + yts.errors
        if status == "ok" and (errors or len(lines) < 0.9 * expect):
            status = "partial"
        state["last_pass"] = {"at": ypf._iso(now_ts), "status": status, "videos": len(lines),
                              "tracked": expect, "candidates": len(cands),
                              "unmatched": len(unmatched) - found,
                              "calls": yt.calls + yts.calls, "discover_calls": disc_calls,
                              "added": added, "dropped": dropped, "errors": errors[:5]}
        ypf._write_json(IDS_FILE, ids)
        ypf._write_json(STATE_FILE, state)
    for e in (yt.errors + yts.errors)[:5]:
        log(f"! {e}")
    summary = (f"snapshot {len(lines)}/{expect} videos, {len(cands)} candidates"
               + (f" ({len(unmatched) - found} unmatched)" if len(unmatched) > found else "")
               + ("" if disc_calls is None else f" | discover {disc_calls} calls, +{added} new")
               + (f" | {found} found by search" if found else "")
               + (f" | {dropped} dropped" if dropped else "")
               + f" | {yt.calls + yts.calls} calls, {ledger.units}/{DAILY_UNITS} units PT {ledger.q['day']}"
               + kalshi_note + ("" if status == "ok" else f" | {status}"))
    log(summary)
    if status.startswith("quota"):
        log(f"! the key's daily quota ran out: blocked to {ypf._iso(state['blocked_until'])}")
    return (0 if status == "ok" and not kalshi_note else 1), summary


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
    state = ypf._read_json(STATE_FILE) or {}
    ids = load_ids()
    last = float(state.get("last_snapshot") or 0)
    q = state.get("quota") or {}
    try:
        size = os.path.getsize(OUT_FILE)
    except OSError:
        size = 0
    open_evs = sorted(ids["events"])[-4:]
    out = [f"tracked videos {len(ids['videos'])}; events mapped {len(ids['events'])} "
           f"(latest {', '.join(open_evs) or '-'})",
           f"last snapshot {ypf._iso(last) if last else 'never'}"
           + (f" ({(now_ts - last) / 60:.0f} min ago)" if last else ""),
           f"last discover {ypf._iso(float(state['last_discover'])) if state.get('last_discover') else 'never'}",
           f"units PT {q.get('day', '-')}: {q.get('units', 0)}/{DAILY_UNITS}; searches "
           f"{(state.get('searches') or {}).get('n', 0)}/{SEARCH_PER_DAY}",
           f"last pass {json.dumps(state.get('last_pass'))}",
           f"{OUT_FILE}: {size / 1e6:.1f} MB"]
    if float(state.get("blocked_until") or 0) > now_ts:
        out.append(f"BLOCKED (key quota) to {ypf._iso(float(state['blocked_until']))}")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="One pass of the global top-video YouTube API feed.")
    ap.add_argument("--force", action="store_true", help=f"ignore the {MIN_GAP_MIN:g}-min gap guard")
    ap.add_argument("--discover", action="store_true", help="run the mostPopular discovery this pass")
    ap.add_argument("--dry-run", action="store_true",
                    help="plan only: no key read, no API call, nothing written")
    ap.add_argument("--status", action="store_true", help="print the feed's state and exit")
    a = ap.parse_args(argv)
    if a.status:
        print(status_text())
        return 0
    if a.dry_run:
        ids = load_ids()
        n = len(ids["videos"])
        if not ids.get("seeded_from_collector"):
            src = ypf._read_json(SOURCE_IDS_FILE)
            n += sum(1 for r in (src or {}).values()
                     if isinstance(r, dict) and r.get("id") and r["id"] not in ids["videos"])
        state = ypf._read_json(STATE_FILE) or {}
        print(f"{n} videos -> {math.ceil(n / BATCH)} videos.list calls; discover due: "
              f"{time.time() - float(state.get('last_discover') or 0) >= DISCOVER_EVERY_H * 3600 - 600}; "
              f"budget {DAILY_UNITS}/PT day; out {OUT_FILE}")
        return 0
    lock = single_instance()
    if lock is None:
        log("another pass holds the lock; exiting")
        return 3
    try:
        code, _ = run_pass(force=a.force, force_discover=a.discover)
        return code
    except Exception as e:
        log(f"! pass failed: {type(e).__name__}: {ypf.scrub(str(e))[:200]}")
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
