#!/usr/bin/env python3
"""Live page for the IMM's crypto "after economic events" strikes (Jack
2026-11-14: "create a simple dash like this for the crypto after events",
pointing at the wave page on :8782). Read-only, like the page it reuses.

imm_pvd_watch.py's page, poll and charts, fed a different market list:
  * MARKETS: every market of the four *EVENT series (KXBTCEVENT, KXETHEVENT,
    KXSOLEVENT, KXHYPEEVENT; CRYPTO_WATCH_SERIES overrides) with a live
    liquidity program on Kalshi's /incentive_programs, re-read every 30 min.
    Pool $/day = the latest period's reward over its length (pvd_scan's rule).
  * STOP: the program's end, or 00:00 ET on the event's day if sooner (the
    bot's cutoff for these strikes).
  * LIMITS: the crypto event gate's caps off the bot's crypto_event_fair.json
    -- bids at most p_lo - tol, asks at least p_hi + tol.
  * HISTORY in run-logs/crypto-event-watch; go-live 2026-11-14 01:09:35Z (the
    restart that put both sides up, 1442295) unless PVD_WATCH_GO_LIVE says.

    python imm_crypto_event_watch.py [--port 8783]
then open http://127.0.0.1:8783/ .
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("PVD_WATCH_DIR", os.path.join(HERE, "run-logs", "crypto-event-watch"))
os.environ.setdefault("PVD_WATCH_GO_LIVE", "2026-11-14T01:09:35Z")
os.environ.setdefault("PVD_WATCH_PACE_BAR", "1.10")
sys.path.insert(0, HERE)
import imm_pvd_watch as w  # noqa: E402

SERIES = [s.strip() for s in (os.environ.get("CRYPTO_WATCH_SERIES") or
                              "KXBTCEVENT,KXETHEVENT,KXSOLEVENT,KXHYPEEVENT").split(",") if s.strip()]
FAIR_FILE = os.path.join(HERE, "run-logs", "incentive-mm", "crypto_event_fair.json")
PROGRAM_SECS = 1800.0
_SEG = re.compile(r"^(\d{2})([A-Z]{3})(\d{2})(\d{4})?$")       # 09DEC261530
_MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}
_progs: dict = {"at": 0.0, "markets": {}}


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def event_day_cutoff(event: str):
    """00:00 ET on the event's day (KXBTCEVENT-09DEC261530 -> Dec 9), or None."""
    m = _SEG.match(event.partition("-")[2])
    if not m or m.group(2) not in _MON:
        return None
    try:
        d = datetime(2000 + int(m.group(3)), _MON[m.group(2)], int(m.group(1)))
    except ValueError:
        return None
    return w.imm.ET.localize(d).astimezone(timezone.utc)


def read_programs(now: datetime) -> dict:
    """ticker -> {pool_day, start, end} for the SERIES' live liquidity programs."""
    if _progs["markets"] and time.time() - _progs["at"] < PROGRAM_SECS:
        return _progs["markets"]
    per: dict = {}
    cursor = None
    for _ in range(40):
        params = {"limit": 1000, "status": "active"}
        if cursor:
            params["cursor"] = cursor
        r = w.kalshi_reads.read("/incentive_programs", params)
        batch = r.get("incentive_programs") or []
        for p in batch:
            t = p.get("market_ticker") or ""
            if t.split("-")[0] in SERIES and p.get("incentive_type") == "liquidity" \
                    and _ts(p.get("end_date")) and _ts(p.get("start_date")) \
                    and _ts(p["end_date"]) > now:
                per.setdefault(t, []).append(p)
        cursor = r.get("next_cursor")
        if not cursor or not batch:
            break
    out = {}
    for t, ps in per.items():
        end = max(_ts(p["end_date"]) for p in ps)
        cur = [p for p in ps if _ts(p["end_date"]) == end]
        st = min(_ts(p["start_date"]) for p in cur)
        days = max((end - st).total_seconds() / 86400, 1 / 24)
        out[t] = {"pool_day": sum(float(p.get("period_reward") or 0) / 10000 for p in cur) / days,
                  "start": st, "end": end}
    _progs.update(at=time.time(), markets=out)
    return out


def _caps() -> dict:
    """ticker -> (bid cap, ask floor) from the bot's crypto event fair."""
    try:
        with open(FAIR_FILE, encoding="utf-8") as fh:
            mk = (json.load(fh) or {}).get("markets") or {}
    except (OSError, ValueError):
        return {}
    tol = w.imm.CRYPTO_EVENT_TOL_CENTS
    out = {}
    for t, e in mk.items():
        if "p_lo" in e and "p_hi" in e:
            b = math.floor(100 * e["p_lo"] - tol + 1e-9)
            a = math.ceil(100 * e["p_hi"] + tol - 1e-9)
            out[t] = (b if b >= 1 else None, a if a <= 99 else None)
    return out


def load_list() -> dict:
    now = datetime.now(timezone.utc)
    progs = read_programs(now)
    caps = _caps()
    out = {}
    for t, p in progs.items():
        ev = t.rsplit("-", 1)[0]
        stop = p["end"]
        cut = event_day_cutoff(ev)
        if cut is not None and cut < stop:
            stop = cut
        bmax, amin = caps.get(t, (None, None))
        out[t] = {"event": ev, "stop": _iso(stop), "pool_day": round(p["pool_day"], 4),
                  "period_start": _iso(p["start"]), "kind": t.split("-")[0][2:-5],
                  "size": 200, "bid_max": bmax, "ask_min": amin, "added": ""}
    return out


_orig_poll = w.poll


def poll(client, hist: dict) -> dict:
    d = _orig_poll(client, hist)
    for r in d.get("rows") or []:
        if r.get("src") == "11/04 list":
            r["src"] = r.get("event") or "crypto fair"
    for b in d.get("by_src") or []:
        if b.get("src") == "11/04 list":
            b["src"] = "crypto events"
    return d


w.load_list = load_list
w.poll = poll
# no wave scanner line: pvd_scan.py lists dead books, not these
w.SCAN_FILE = os.path.join(HERE, "run-logs", "crypto-event-watch", "no_scan.json")
w.PAGE = (w.PAGE
          .replace("<title>Dead Books Live</title>", "<title>Crypto Events Live</title>")
          .replace("<h1>Post-vote dead books</h1>",
                   "<h1>Crypto after economic events</h1>")
          .replace(".tag{display:inline-block;", ".tag{background:var(--live);display:inline-block;")
          # kind = the coin here: the dead/live section is a by-coin table,
          # and its dead-vs-live chart (kchart) has nothing to draw -- hidden,
          # not removed, so the page script still finds it
          .replace("<h2>Dead books vs live books</h2>", "<h2>By coin</h2>")
          .replace('<div class="wrap" style="padding:8px 12px;margin-top:8px"><div class="mut key">'
                   'Net (est rewards + trading) by kind, cumulative:',
                   '<div class="wrap" style="display:none"><div class="mut key">'
                   'Net (est rewards + trading) by kind, cumulative:')
          .replace("(11/04 list = the hand-built list; scan = added by pvd_scan.py every 15 min: "
                   "new programs, relit periods, extended stops)",
                   "(every market with a program; limits = the crypto event fair's band caps)"))

if __name__ == "__main__":
    if "--port" not in sys.argv:
        sys.argv += ["--port", "8783"]
    w.main()
