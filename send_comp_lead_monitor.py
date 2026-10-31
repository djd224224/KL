#!/usr/bin/env python3
r"""send_comp_lead_monitor.py -- the daily complement-lead and hedge email
(Jack 2026-10-29: "I mean just a daily status email report on this
mechanism").

THE MECHANISM (incentive_mm COMP_LEAD_MODE / COMP_HEDGE_MODE, a042520): in an
event Kalshi marks mutually exclusive, a side whose own book sits far behind
the price its sibling markets imply LEADS with one rung of target/5 (200) at
the cheapest price that takes ~95% of that side's reward score -- the sell
side in any family, the buy side in two-market election pairs. A lead that
fills is hedged on its siblings once the set locks a profit; an order resting
ahead of a lead is fought to a 5c stop, since 2026-10-30 only once it leaves
the lead under half of its side (Jack: "Hold at one half").

Since 2026-10-29 (Jack: "1 and 2") every maker fill that opens a position
in such an event, or on a threshold ladder ("above X" strikes of one
number), is locked the same way once the hedge clears 2c a contract after
fees (a lead's fill: any floor), and ladder strikes lead off their
neighbours' books. The hedge legs are resting MAKER orders only (Jack:
"Don't be a taker, only maker"), cancelled once the lock completes or the
job ends.

Since 2026-10-29 (Jack: "since I have a fair we should be willing to come in
above the wall with up to 200 contracts") weekend rain's sides also lead off
its MODEL FAIR (incentive_mm FAIR_LEAD_MODE): 200 at the cheapest price that
takes ~95% of the side, at least FAIR_LEAD_MARGIN_C inside the fair (10c,
then 5c from 2026-10-30: "be willing to go to 5c of the model if
necessary"). Those log `fair lead`
lines, get their own section, and their fills ride as inventory (nothing
hedges a city's market), so they never count toward the hedge verdict.
Since 2026-10-30 (Jack: "Yes, extend it") the Vercel family leads off its
fair the same way; a Vercel strike's fill is a ladder strike's, so it opens
a lock job off the other strikes like any lead fill there. So does the
OpenRouter weekly (KXTOKENUSE, Jack: "Yes, add it"), whose leads log `off`
for OpenRouter's 00:00Z publish and come back once the new day is read.

ONE EMAIL per ET day (the day before), THE TAKEAWAY FIRST (Jack 2026-10-30:
"really $0 for complement lead? clean up this email and make it clear the
takeaway" -- the old subject carried only the locks' dollars, $0 that day,
while leading had earned ~$15 and its fills were up ~$38):
  subject     earned $X while leading, N lead fills $Y now, locks $Z, net,
              then the verdict and, if not OK, its reason in a few words
  takeaway    the same per kind (complement / fair): sides and markets led,
              the reward the markets accrued while leading (the bot's own
              hourly estimate over the lead spans, imm_reward_recon's cache;
              Kalshi pays ~1.15x it), the fills at a lead marked to now
  needs a look  each verdict reason, naming the market and what is left
              (PROBLEM: a lead fill never locked, Kalshi refused a hedge
              order, or a lock that gave up with legs traded or has been half
              done for over HEDGE_STUCK_SECS can lose LOCK_PROBLEM_USD ($10)
              or more as it stands; WATCH: such a lock that can lose less, a
              complement-lead fill that opened no lock job, or a lock job
              still working). Each such lock shows its worst and best case in
              dollars over how its markets can settle (Jack 2026-10-30: "Yes,
              size-aware" -- 10/29's 12-contract lock read PROBLEM at -$2.68)
  top earners the markets that earned most while leading
  fills       every fill at a lead's price, marked to the bot's latest mark
              or the settlement, and what became of it (locked, a lock job
              open, no lock; a fair lead's rides to settlement)
  locks       every other fill watched for a lock: locked (and the dollars),
              unwound by the bot first, or ridden on as inventory
  counts, then every lead by event (those that led LIST_MIN_HOURS or
              earned LIST_MIN_USD; one line an event)

SOURCES, read-only: the bot's day logs (incentive-mm-*.log), fills_ /
comp_hedge_ / selection_snapshot_ / settlements_<UTC date>.jsonl,
comp_hedge_jobs.json, reward_est_hourly_cache.json and the newest
marks_<date>.jsonl's tail. No Kalshi reads, no orders. Imports
imm_quote_gaps first, like the other morning emails, so the launcher env and
the alert credentials apply.

USAGE:
  python send_comp_lead_monitor.py                   # the scheduled daily run
  python send_comp_lead_monitor.py --dry             # build + print, send nothing
  python send_comp_lead_monitor.py --test            # send now, ignore the marker
  python send_comp_lead_monitor.py --day 2026-10-29  # a specific ET day
Scheduled daily as "KL imm comp lead" (register_imm_comp_lead.ps1), 07:55 ET.
Sends every morning, so a silent day is distinguishable from a broken task.
All console output is ASCII (the task console is cp1252).
"""
import argparse
import ctypes
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_quote_gaps as _gaps  # noqa: F401,E402  (import side effects)
import incentive_mm as imm  # noqa: E402
from incentive_mm import log  # noqa: E402

LOOKBACK_SECS = 6 * 3600        # day-log read starts this far before the window
HEDGE_STUCK_SECS = 3600         # an open job part-hedged this long needs a look...
LOCK_PROBLEM_USD = 10.0         # ...a PROBLEM when it can lose this much as it stands (or
                                # cannot be priced), else a WATCH; a lock that gave up too
LEVELS = ("OK", "WATCH", "PROBLEM")
HOURLY_CACHE_FILE = "reward_est_hourly_cache.json"   # in STATUS_DIR (imm_reward_recon)
MARKS_TAIL_BYTES = 4_000_000    # the newest marks file's tail: every position's last mark
TOP_EARNERS = 10
LIST_MIN_HOURS = 0.5            # ALL LEADS lists a lead that led this long...
LIST_MIN_USD = 0.05             # ...or earned this much while leading

_LEAD = re.compile(
    r"^(\S+ \S+)Z \[IMM\] (comp|fair) lead( \(dry\))? (\S+) (bid|ask): (\d+) @ (\d+)c, "
    r"(?:ahead of|held at the cap, (?:with|behind)) its own (\d+)c; (.+?) "
    r"(?:implies|imply) (-?\d+)c")
_OFF = re.compile(r"^(\S+ \S+)Z \[IMM\] (?:comp|fair) lead(?: \(dry\))? (\S+) (bid|ask): "
                  r"off -- ")
_GROUPS = re.compile(r"^(\S+ \S+)Z \[IMM\] comp lead: (\d+) mutually exclusive "
                     r"event\(s\) grouped, (\d+) of them two-market")
_BANNER = re.compile(r"^(\S+ \S+)Z === incentive_mm")


def low_priority() -> None:
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40)      # IDLE_PRIORITY_CLASS
    except Exception:
        pass


def _ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def day_window(day) -> tuple:
    """[00:00 ET, next 00:00 ET) of an ET date, as UTC epoch seconds."""
    a = imm.ET.localize(datetime(day.year, day.month, day.day))
    b = imm.ET.localize(datetime(day.year, day.month, day.day) + timedelta(days=1))
    return a.timestamp(), b.timestamp()


def utc_dates(a_ts: float, b_ts: float) -> list:
    d = datetime.fromtimestamp(a_ts, timezone.utc).date()
    last = datetime.fromtimestamp(b_ts - 1, timezone.utc).date()
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def day_log_lines(a_ts: float):
    """Lines of every day log that can hold the window, oldest file first."""
    for fn in sorted(glob.glob(os.path.join(imm.STATUS_DIR, "incentive-mm-*.log"))):
        try:
            if os.path.getmtime(fn) < a_ts - LOOKBACK_SECS:
                continue
        except OSError:
            continue
        with open(fn, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line


def jsonl_rows(prefix: str, a_ts: float, b_ts: float):
    """Rows of STATUS_DIR/<prefix>_<UTC date>.jsonl covering the window."""
    for d in utc_dates(a_ts, b_ts):
        try:
            with open(os.path.join(imm.STATUS_DIR, f"{prefix}_{d}.jsonl"),
                      encoding="utf-8") as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            continue


def row_ts(r: dict) -> float:
    v = r.get("ts")
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_leads(lines, a_ts: float, b_ts: float) -> dict:
    """{(ticker, side): {"spans": [[start, end], ...], "px", "best", "implied",
    "who", "dry", "open", "kind"}} for the window from the day-log lines
    (kind "comp" or "fair"). A restart banner closes every open lead (a new
    process logs its leads afresh)."""
    leads: dict = {}
    groups = None
    for line in lines:
        m = _LEAD.match(line)
        if m:
            ts = _ts(m.group(1))
            if ts >= b_ts:
                continue
            key = (m.group(4), m.group(5))
            ld = leads.setdefault(key, {"spans": [], "open": None, "pxs": []})
            if ld["open"] is None:
                ld["open"] = ts
            if a_ts <= ts:
                ld["pxs"].append(int(m.group(7)))
            ld.update(px=int(m.group(7)), best=int(m.group(8)), who=m.group(9),
                      implied=int(m.group(10)), dry=bool(m.group(3)), size=int(m.group(6)),
                      kind=m.group(2))
            continue
        m = _OFF.match(line) or _BANNER.match(line)
        if m:
            ts = _ts(m.group(1))
            if ts >= b_ts:
                continue
            keys = ([(m.group(2), m.group(3))] if m.re is _OFF
                    else [k for k, v in leads.items() if v["open"] is not None])
            for key in keys:
                ld = leads.get(key)
                if ld and ld["open"] is not None:
                    ld["spans"].append([ld["open"], ts])
                    ld["open"] = None
            continue
        m = _GROUPS.match(line)
        if m and _ts(m.group(1)) < b_ts:
            groups = (int(m.group(2)), int(m.group(3)))
    out = {}
    for key, ld in leads.items():
        spans = [[max(s, a_ts), min(e, b_ts)] for s, e in ld["spans"] + (
            [[ld["open"], b_ts]] if ld["open"] is not None else []) if e > a_ts]
        spans = [sp for sp in spans if sp[1] > sp[0]]
        if spans:
            out[key] = dict(ld, spans=spans, leading_at_end=ld["open"] is not None)
    return {"leads": out, "groups": groups}


def lead_fills(fills: list, leads: dict) -> list:
    """Our maker fills on a side while it led, at the price it led at."""
    out = []
    for f in fills:
        if f.get("is_taker"):
            continue
        side = f.get("our_book_side") or (
            "bid" if (f.get("side") == "yes") == (f.get("action") == "buy") else "ask")
        ld = leads.get((f.get("ticker"), side))
        if not ld:
            continue
        ts = row_ts(f)
        px = f.get("yes_price_cents")
        if any(s - 60 <= ts <= e + 600 for s, e in ld["spans"]) and px is not None \
                and abs(float(px) - ld["px"]) <= 0.5:
            out.append(dict(ticker=f.get("ticker"), side=side, ts=ts,
                            count=float(f.get("count") or 0), px=float(px),
                            kind=ld.get("kind", "comp")))
    return out


def lock_outcomes(kind, price, count, legs):
    """(worst, best) dollars, over how its markets can settle, of a lock's
    fill (`count` at `price`, YES cents) plus the legs it has traded so far
    (`legs` {ticker: (contracts, YES-cent value, fees in cents)}); the worst
    is the bot's lock floor for the whole fill (incentive_mm lock_plan). None
    for a lock kind it cannot price."""
    q, px = float(count or 0.0), float(price or 0.0)
    n = [float(c) for c, _v, _f in legs.values()]
    val = sum(float(v) for _c, v, _f in legs.values())
    fee = sum(float(f) for _c, _v, f in legs.values())
    if kind == "me_ask":
        # sold YES here and on siblings: at most one of them wins
        got = px * q + val - fee
        worst, best = got - 100.0 * max([q] + n), got
    elif kind == "pair_bid":
        # bought YES here and on the twin: one of the two wins (a tie aside)
        cost = px * q + val + fee
        worst, best = 100.0 * min(q, sum(n)) - cost, 100.0 * max(q, sum(n)) - cost
    elif kind == "ladder_ask":
        # sold YES here, bought it on easier strikes (YES whenever this is)
        got = px * q - val - fee
        worst, best = got - 100.0 * max(0.0, q - sum(n)), got + 100.0 * sum(n)
    elif kind == "ladder_bid":
        # bought YES here, sold it on harder strikes (YES only when this is)
        got = val - px * q - fee
        worst, best = got + min(0.0, 100.0 * (q - sum(n))), got + 100.0 * q
    else:
        return None
    return worst / 100.0, best / 100.0


def hedge_summary(rows: list, jobs: list, now_ts: float) -> dict:
    """The day's lock rows (comp_hedge_<date>.jsonl) and the open jobs now. A
    row without a `lead` flag predates the every-fill locks: a lead's."""
    def is_lead(r):
        return r.get("lead", True) is not False
    opened = [r for r in rows if r.get("kind") == "open" and is_lead(r)]
    done = [r for r in rows if r.get("kind") == "done" and is_lead(r)]
    expired = [r for r in rows if r.get("kind") == "expired"]
    never = [r for r in rows if r.get("kind") == "nolock" and is_lead(r)]
    sends = [r for r in rows if r.get("kind") == "send" and not r.get("dry")]
    # a post-only leg the book moved into is refused as a cross: routine,
    # the leg is priced again; anything else Kalshi refuses is a problem
    refused = [r for r in sends if r.get("error")
               and "cross" not in str(r.get("error_body") or r.get("error")).lower()]
    crossed = [r for r in sends if r.get("error") and r not in refused]
    leg_fills = [r for r in rows if r.get("kind") == "leg_fill"]
    locked = sum(float(r.get("edge_c") or 0.0) * float(r.get("count") or 0.0) / 100.0
                 for r in done)
    # every other fill watched for a lock
    l_open = [r for r in rows if r.get("kind") == "open" and not is_lead(r)]
    l_done = [r for r in rows if r.get("kind") == "done" and not is_lead(r)]
    l_none = [r for r in rows if r.get("kind") == "nolock" and not is_lead(r)]
    l_unwound = [r for r in rows if r.get("kind") == "unwound"]
    l_locked = sum(float(r.get("edge_c") or 0.0) * float(r.get("count") or 0.0) / 100.0
                   for r in l_done)
    by_kind: dict = defaultdict(lambda: [0, 0])
    for r in l_open:
        by_kind[r.get("lock") or "?"][0] += 1
    for r in l_done:
        by_kind[r.get("lock") or "?"][1] += 1
    open_now = []
    for j in jobs:
        q = float(j.get("count") or 0.0)
        lead = j.get("lead", True) is not False
        traded = {s: float(lg.get("filled", 0.0)) for s, lg in (j.get("legs") or {}).items()
                  if float(lg.get("filled", 0.0)) >= 0.5}
        if not lead and not traded:
            continue          # a fill still in its lock watch: nothing to report
        # what it stands to make or lose with the legs traded so far
        legs = {s: lg for s, lg in (j.get("legs") or {}).items()
                if float(lg.get("filled", 0.0)) > 0}
        outcome = (lock_outcomes(j.get("kind"), j.get("price"), q,
                                 {s: (lg["filled"], lg["value"], lg.get("fee", 0.0))
                                  for s, lg in legs.items()})
                   if all("value" in lg for lg in legs.values()) else None)
        open_now.append(dict(id=j.get("id"), ticker=j.get("ticker"), side=j.get("side"),
                             kind=j.get("kind"), lead=lead, count=q, price=j.get("price"),
                             age_s=now_ts - float(j.get("at") or now_ts),
                             traded=traded, best=j.get("best"), outcome=outcome,
                             waiting=bool(j.get("waiting")), until=j.get("until")))
    return dict(opened=opened, done=done, expired=expired, never=never, sends=sends,
                refused=refused, crossed=crossed, leg_fills=leg_fills, locked=locked,
                open_now=open_now,
                locks=dict(opened=l_open, done=l_done, none=l_none, unwound=l_unwound,
                           locked=l_locked, by_kind=dict(by_kind)))


def gave_up_outcome(r: dict):
    """lock_outcomes of a lock that gave up (an `expired` row, which keeps no
    prices), rebuilt from its job's rows: the fill from its `open` row, the
    legs from its `leg_fill` rows with the fee the bot books on each. A job's
    id ends with the epoch second it opened. None when its open row is not
    found. (A job cut to what its market still held before any leg traded
    is priced at its opening count: its worst case reads deeper.)"""
    try:
        at = float(str(r.get("job")).rsplit("-", 1)[-1])
    except ValueError:
        return None
    opened, legs = None, {}
    for x in jsonl_rows("comp_hedge", at - 60, row_ts(r) or time.time()):
        if x.get("job") != r.get("job"):
            continue
        if x.get("kind") == "open":
            opened = x
        elif x.get("kind") == "leg_fill":
            n, px = float(x.get("count") or 0.0), float(x.get("px") or 0.0)
            c, v, f = legs.get(x.get("ticker"), (0.0, 0.0, 0.0))
            legs[x.get("ticker")] = (c + n, v + n * px, f + imm.taker_fee_cents(
                px, n, imm.LOCK_MAKER_FEE_MULT))
    if opened is None:
        return None
    # (a job opened before its event's group was read gets its kind later)
    return lock_outcomes(r.get("lock") or opened.get("lock"), opened.get("px"),
                         opened.get("count"), legs)


def latest_estimates(tickers: set, a_ts: float, b_ts: float) -> dict:
    """ticker -> (est $/day, est share) from the latest selection snapshot rows."""
    out = {}
    for r in jsonl_rows("selection_snapshot", a_ts, b_ts + 86400):
        t = r.get("ticker")
        if t in tickers:
            out[t] = (float(r.get("est_dollars_per_day") or 0.0),
                      float(r.get("est_frac") or 0.0))
    return out


def load_jobs() -> list:
    try:
        with open(os.path.join(imm.STATUS_DIR, imm.COMP_HEDGE_FILE), encoding="utf-8") as f:
            return [j for j in (json.load(f).get("jobs") or []) if isinstance(j, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def load_hourly(a_ts: float, b_ts: float) -> tuple:
    """({ticker: {UTC hour index: est $}}, covered-until ts) from
    imm_reward_recon's hourly cache -- the bot's own reward estimate, cycle
    by cycle, bucketed by hour -- for the day files holding the window.
    ({}, None) without the cache."""
    try:
        with open(os.path.join(imm.STATUS_DIR, HOURLY_CACHE_FILE), encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        return {}, None
    out: dict = {}
    last = None
    for d in utc_dates(a_ts, b_ts):
        for t, hs in ((cache.get(f"cycle_log_{d}.csv") or {}).get("markets") or {}).items():
            dd = out.setdefault(t, {})
            for h, v in hs.items():
                h = int(h)
                dd[h] = dd.get(h, 0.0) + float(v)
                last = h if last is None else max(last, h)
    return out, (None if last is None else (last + 1) * 3600.0)


def _union(spans) -> list:
    out: list = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def lead_rewards(leads: dict, hourly: dict) -> dict:
    """{ticker: est $ the market accrued while one of its sides led} -- its
    hourly estimate over the union of its lead spans, pro rata within an
    hour (both sides leading at once count once)."""
    spans: dict = defaultdict(list)
    for (t, _side), v in leads.items():
        spans[t].extend(v["spans"])
    out = {}
    for t, sp in spans.items():
        hs = hourly.get(t) or {}
        tot = 0.0
        for s, e in _union(sp):
            h = int(s // 3600)
            while h * 3600 < e:
                ov = min(e, (h + 1) * 3600.0) - max(s, h * 3600.0)
                if ov > 0:
                    tot += hs.get(h, 0.0) * ov / 3600.0
                h += 1
        out[t] = tot
    return out


def load_marks(a_ts: float, now_ts: float) -> dict:
    """{ticker: mark cents now}: the settlement where a market has settled,
    else the bot's latest mark (the tail of the newest marks_<date>.jsonl,
    which re-marks every open position each pass)."""
    out: dict = {}
    files = sorted(glob.glob(os.path.join(imm.STATUS_DIR, "marks_*.jsonl")))
    if files:
        try:
            with open(files[-1], "rb") as fh:
                fh.seek(max(0, os.path.getsize(files[-1]) - MARKS_TAIL_BYTES))
                tail = fh.read().decode("utf-8", "replace").splitlines()[1:]
        except OSError:
            tail = []
        for line in tail:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("ticker") and r.get("mark_cents") is not None:
                out[r["ticker"]] = float(r["mark_cents"])
    for r in jsonl_rows("settlements", a_ts, now_ts):
        if r.get("result") in ("yes", "no"):
            out[r.get("ticker")] = 100.0 if r["result"] == "yes" else 0.0
    return out


def value_fills(fills: list, marks: dict) -> list:
    """Each fill with `mark` (cents now, or None) and `usd`: what it is worth
    at that mark (+ = made money)."""
    for f in fills:
        m = marks.get(f["ticker"])
        f["mark"] = m
        f["usd"] = (None if m is None else
                    (m - f["px"]) * f["count"] / 100.0 * (1 if f["side"] == "bid" else -1))
    return fills


def _age(secs: float) -> str:
    return f"{secs / 3600:.0f}h" if secs >= 5400 else f"{secs / 60:.0f} min"


def _et(ts: float) -> str:
    return f"{datetime.fromtimestamp(ts, timezone.utc).astimezone(imm.ET):%a %H:%M} ET"


def _until(j: dict, ctx: dict) -> str:
    """When an open lock job gives up -- or that its watch already ended and
    it is held open by the legs it has traded."""
    if not j.get("until"):
        return ""
    until = float(j["until"])
    try:
        now = datetime.fromisoformat(str(ctx.get("now"))).timestamp()
    except ValueError:
        now = time.time()
    if until > now:
        return f"; it gives up {_et(until)}"
    return f"; its watch ended {_et(until)}, so it stays open only for the legs it has traded"


def _stuck(h: dict) -> list:
    """The open lock jobs half done (a leg traded) for over HEDGE_STUCK_SECS."""
    return [j for j in h["open_now"] if j["traded"] and j["age_s"] >= HEDGE_STUCK_SECS]


def _lock_level(outcome) -> int:
    """A half-done or given-up lock: PROBLEM (2) when, as it stands, it can
    lose LOCK_PROBLEM_USD or more, or it cannot be priced; WATCH (1) under
    that (Jack 2026-10-30: "Yes, size-aware")."""
    return 2 if outcome is None or outcome[0] <= -LOCK_PROBLEM_USD else 1


def _sized(outcome) -> str:
    if outcome is None:
        return "; its dollars could not be priced"
    return f"; as it stands {usd(outcome[0])} at worst, {usd(outcome[1])} at best"


def verdict(ctx: dict) -> tuple:
    """(level, reasons): what needs a look, each reason naming the market and
    what is left. PROBLEM: a lead fill never locked, Kalshi refused a hedge
    order, or a lock that gave up with legs traded or has been half done for
    over HEDGE_STUCK_SECS can lose LOCK_PROBLEM_USD or more as it stands (or
    cannot be priced); WATCH: such a lock that can lose less, a
    complement-lead fill that opened no lock job, or a lock job still
    working; OK otherwise."""
    reasons, level = [], 0

    def flag(lv, why):
        nonlocal level
        level = max(level, lv)
        reasons.append(why)

    h = ctx["hedges"]
    for r in h["expired"]:
        oc = r.get("outcome")
        flag(_lock_level(oc), f"{r.get('ticker') or r.get('job')}: its lock gave up with legs "
                              f"traded and no full lock -- the traded legs now ride as "
                              f"inventory" + _sized(oc))
    if h["never"]:
        flag(2, f"{len(h['never'])} lead fill(s) were never locked: " + ", ".join(
            sorted({str(r.get("ticker") or r.get("job")) for r in h["never"]})))
    if h["refused"]:
        r0 = h["refused"][0]
        flag(2, f"Kalshi refused {len(h['refused'])} hedge order(s) (first: "
                f"{r0.get('ticker')} {r0.get('side')} {r0.get('count')} @ {r0.get('px')}c: "
                f"{r0.get('error_body') or r0.get('error')})")
    stuck = _stuck(h)
    for j in stuck:
        legs = ", ".join(f"{q:g} of {s}" for s, q in j["traded"].items())
        what = "sold" if j.get("side") == "ask" else "bought"
        best = j.get("best")
        oc = j.get("outcome")
        flag(_lock_level(oc),
             f"{j.get('ticker')}: the lock on a fill ({what} {float(j.get('count') or 0):g} @ "
             f"{float(j.get('price') or 0):g}c) has been half done for {_age(j['age_s'])} -- "
             f"{legs} traded, the other legs not" + _sized(oc)
             + (f"; a full lock priced at up to {float(best):+.1f}c a contract"
                if best is not None else "")
             + ("; its legs' books are off the feed" if j.get("waiting") else "")
             + _until(j, ctx))
    # (a FAIR lead's fill has nothing to hedge on: never counted here)
    comp_fills = [f for f in ctx["fills"] if f.get("kind", "comp") == "comp"]
    if len(comp_fills) > len(h["opened"]):
        flag(1, f"{len(comp_fills)} complement-lead fill(s) but only {len(h['opened'])} lock "
                f"job(s) opened")
    young = [j for j in h["open_now"] if j not in stuck]
    if young:
        flag(1, f"{len(young)} lock job(s) still working, none of their legs traded yet: "
                + ", ".join(f"{j.get('ticker')} ({_age(j['age_s'])} old)" for j in young))
    return LEVELS[level], reasons


def _short(ctx) -> str:
    """The verdict in a few words, for the subject."""
    h = ctx["hedges"]
    stuck = _stuck(h)
    young = [j for j in h["open_now"] if j not in stuck]
    bits = []

    def worst(locks) -> str:
        known = [x["outcome"][0] for x in locks if x.get("outcome") is not None]
        return f" (worst {usd(sum(known))})" if known else ""
    if h["expired"]:
        bits.append(f"{len(h['expired'])} lock gave up" + worst(h["expired"]))
    if h["never"]:
        bits.append(f"{len(h['never'])} lead fill never locked")
    if h["refused"]:
        bits.append(f"{len(h['refused'])} hedge order refused")
    if stuck:
        bits.append(f"{len(stuck)} lock half done {_age(max(j['age_s'] for j in stuck))}"
                    + worst(stuck))
    if young and not bits:
        bits.append(f"{len(young)} lock job working")
    return ", ".join(bits)


def usd(x: float) -> str:
    return ("-$" if x < 0 else "+$") + f"{abs(x):,.2f}"


def build_report(day, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    a_ts, b_ts = day_window(day)
    parsed = parse_leads(day_log_lines(a_ts), a_ts, b_ts)
    leads = parsed["leads"]
    fills = lead_fills(list(jsonl_rows("fills", a_ts, b_ts)), leads)
    rows = [r for r in jsonl_rows("comp_hedge", a_ts, b_ts) if a_ts <= row_ts(r) < b_ts]
    hedges = hedge_summary(rows, load_jobs(), now.timestamp())
    for r in hedges["expired"]:
        r["outcome"] = gave_up_outcome(r)
    est = latest_estimates({t for t, _s in leads}, a_ts, b_ts)
    hourly, covered = load_hourly(a_ts, b_ts)
    value_fills(fills, load_marks(a_ts, now.timestamp()))
    ctx = dict(day=day.isoformat(), leads=leads, groups=parsed["groups"], fills=fills,
               hedges=hedges, est=est, now=now.isoformat(),
               rewards=lead_rewards(leads, hourly), covered_until=covered, window_end=b_ts)
    ctx["level"], ctx["reasons"] = verdict(ctx)
    return ctx


def _split(ctx) -> tuple:
    """(complement leads, fair leads, complement-lead fills, fair-lead fills)."""
    comp = {k: v for k, v in ctx["leads"].items() if v.get("kind", "comp") == "comp"}
    fair = {k: v for k, v in ctx["leads"].items() if v.get("kind") == "fair"}
    cf = [f for f in ctx["fills"] if f.get("kind", "comp") == "comp"]
    ff = [f for f in ctx["fills"] if f.get("kind") == "fair"]
    return comp, fair, cf, ff


def _locked_sides(ctx) -> set:
    """(ticker, side) of the lead fills whose lock completed: their dollars
    are the lock's, not the fill's mark."""
    out = set()
    for r in ctx["hedges"]["done"]:
        t, side = r.get("ticker"), r.get("side")
        if t and side:
            out.add((t, side))
    return out


def totals(ctx) -> dict:
    """Per kind ("comp", "fair"): sides and markets led, sides leading at the
    day's end, reward while leading, fills, contracts, the fills' dollars at
    their marks (a locked fill counts in "locks" instead) and the fills with
    no mark; plus "locks" (every completed lock) and "net"."""
    rew = ctx.get("rewards") or {}
    locked = _locked_sides(ctx)
    out = {}
    for kind in ("comp", "fair"):
        sides = {k: v for k, v in ctx["leads"].items() if v.get("kind", "comp") == kind}
        tickers = {t for t, _s in sides}
        fl = [f for f in ctx["fills"] if f.get("kind", "comp") == kind]
        priced = [f for f in fl if f.get("usd") is not None
                  and (f.get("ticker"), f.get("side")) not in locked]
        out[kind] = dict(
            sides=len(sides), markets=len(tickers),
            at_end=sum(1 for v in sides.values() if v["leading_at_end"]),
            reward=sum(rew.get(t, 0.0) for t in tickers),
            fills=len(fl), contracts=sum(float(f.get("count") or 0) for f in fl),
            fills_usd=sum(f["usd"] for f in priced),
            unmarked=sum(1 for f in fl if f.get("usd") is None),
            first=min((sp[0] for v in sides.values() for sp in v["spans"]), default=None))
    h = ctx["hedges"]
    out["locks"] = (float(h.get("locked") or 0.0)
                    + float((h.get("locks") or {}).get("locked") or 0.0))
    out["net"] = sum(out[k]["reward"] + out[k]["fills_usd"] for k in ("comp", "fair")) \
        + out["locks"]
    return out


def subject_for(ctx) -> str:
    day = datetime.strptime(ctx["day"], "%Y-%m-%d")
    T = totals(ctx)
    rew = T["comp"]["reward"] + T["fair"]["reward"]
    fills = T["comp"]["fills_usd"] + T["fair"]["fills_usd"]
    nf = T["comp"]["fills"] + T["fair"]["fills"]
    short = _short(ctx) if ctx["level"] != "OK" else ""
    return (f"IMM leads {day:%m/%d}: earned ${rew:,.2f} while leading, "
            f"{nf} lead fill{'s' if nf != 1 else ''} {usd(fills)} now"
            + (f", locks {usd(T['locks'])}" if T["locks"] else "")
            + f", net {usd(T['net'])} | {ctx['level']}" + (f": {short}" if short else ""))


def _lead_line(t: str, side: str, v: dict, reward=None) -> str:
    hrs = sum(e - s for s, e in v["spans"]) / 3600.0
    pxs = v.get("pxs") or []
    moved = f", moved {min(pxs)}-{max(pxs)}c" if pxs and min(pxs) != max(pxs) else ""
    who = v.get("who") or ""
    by = ("fair" if v.get("kind") == "fair" else
          "easier strikes imply" if "easier strikes" in who else
          "harder strikes imply" if "harder strikes" in who else "siblings imply")
    return (f"{t} {side}: {v.get('size', 200)} @ {v['px']}c, own best {v['best']}c, {by} "
            f"{v['implied']}c; led {hrs:.1f}h{moved}" + (" (dry)" if v.get("dry") else "")
            + ("" if v["leading_at_end"] else ", ended")
            + (f"; ${reward:.2f} while leading" if reward is not None else ""))


def render_text(ctx) -> str:
    day = datetime.strptime(ctx["day"], "%Y-%m-%d")
    T = totals(ctx)
    rew = ctx.get("rewards") or {}
    comp, fair, _cfills, _ffills = _split(ctx)
    h = ctx["hedges"]
    lk = h["locks"]
    nf = T["comp"]["fills"] + T["fair"]["fills"]
    out = [f"Leads, {day:%a %m/%d} (ET day) -- complement leads price off other markets' "
           f"books, fair leads off our models (weekend rain, Vercel, OpenRouter weekly)", ""]
    # THE TAKEAWAY: the money, then what (if anything) needs doing
    rew_all = T["comp"]["reward"] + T["fair"]["reward"]
    fills_all = T["comp"]["fills_usd"] + T["fair"]["fills_usd"]
    lock_s = (f"locks completed {usd(T['locks'])}" if T["locks"] else "no lock completed")
    out.append(f"TAKEAWAY: leading earned about ${rew_all:,.2f}; the {nf} fill"
               f"{'s' if nf != 1 else ''} at a lead {'is' if nf == 1 else 'are'} worth "
               f"{usd(fills_all)} now; {lock_s}. Net about {usd(T['net'])}.")
    for kind, label in (("comp", "complement"), ("fair", "fair")):
        k = T[kind]
        if not (k["sides"] or k["fills"]):
            continue
        since = ""
        if k["first"] is not None and k["first"] > day_window(day)[0] + 3600:
            since = f" (from {_et(k['first'])[4:]})"
        unm = f", {k['unmarked']} with no mark" if k["unmarked"] else ""
        out.append(f"  {label:<10s}  {k['sides']} side{'s' if k['sides'] != 1 else ''} on "
                   f"{k['markets']} market{'s' if k['markets'] != 1 else ''}{since}: "
                   f"${k['reward']:,.2f} while leading; {k['fills']} fill"
                   f"{'s' if k['fills'] != 1 else ''} ({k['contracts']:,.0f} contracts) "
                   f"{usd(k['fills_usd'])} now{unm}")
    note = ("  (reward = the bot's own estimate over the hours each market led -- Kalshi "
            "pays ~1.15x it; fills marked to the bot's latest mark or the settlement, not "
            "yet realized")
    cov = ctx.get("covered_until")
    if cov is not None and cov < float(ctx.get("window_end") or 0):
        note += f"; reward counted through {_et(cov)[4:]} only, where the hourly cache stops"
    out.append(note + ")")
    out += ["", f"NEEDS A LOOK ({ctx['level']}):" if ctx["reasons"]
            else "NEEDS A LOOK: nothing."]
    out += [f"  - {r}" for r in ctx["reasons"]]
    if h["expired"] or _stuck(h):
        out.append(f"  (a lock left half done is a PROBLEM when it can lose "
                   f"${LOCK_PROBLEM_USD:,.0f} or more as it stands, a WATCH under that)")
    # where the reward came from
    earners = sorted(((v, t) for t, v in rew.items() if v > 0), reverse=True)
    if earners:
        sides_of: dict = defaultdict(list)
        for (t, side), v in ctx["leads"].items():
            sides_of[t].append((side, v))
        out += ["", f"TOP EARNERS WHILE LEADING ({min(TOP_EARNERS, len(earners))} of "
                    f"{len(rew)} markets)"]
        for v, t in earners[:TOP_EARNERS]:
            sv = sides_of.get(t) or []
            kind = "fair" if any(x.get("kind") == "fair" for _s, x in sv) else "complement"
            what = " + ".join(f"{s} {x['px']}c" for s, x in sorted(sv, key=lambda p: p[0]))
            hrs = sum(e - s for s, e in _union([sp for _s, x in sv for sp in x["spans"]])) / 3600
            out.append(f"  ${v:6.2f}  {t} {what} ({kind}, led {hrs:.1f}h)")
    # the fills, each with what it is worth now
    if ctx["fills"]:
        locked = _locked_sides(ctx)
        open_t = {j.get("ticker") for j in h["open_now"]}
        out += ["", "FILLS AT A LEAD (marked to now)"]
        for f in sorted(ctx["fills"], key=lambda x: x["ts"]):
            when = datetime.fromtimestamp(f["ts"], timezone.utc).astimezone(imm.ET)
            what = "sold" if f["side"] == "ask" else "bought"
            if f.get("kind") == "fair":
                why = "fair: rides to settlement, nothing hedges a city's market"
            elif (f["ticker"], f["side"]) in locked:
                why = "complement: locked (its dollars are in the locks)"
            elif f["ticker"] in open_t:
                why = "complement: lock job still open"
            else:
                why = "complement: no lock"
            val = ("no mark" if f.get("usd") is None else
                   f"now {f['mark']:g}c: {usd(f['usd'])}")
            out.append(f"  {when:%H:%M} ET  {f['ticker']} {what} {f['count']:g} @ "
                       f"{f['px']:g}c, {val}  ({why})")
    out += ["", f"LOCKS ON OTHER FILLS: {len(lk['opened'])} watched, {len(lk['done'])} locked "
                f"({usd(lk['locked'])}), {len(lk['unwound'])} unwound first, "
                f"{len(lk['none'])} rode on as inventory -- a lock needs "
                f"{imm.COMP_HEDGE_EDGE_C:g}c a contract after fees."]
    for kind, (n_open, n_done) in sorted(lk["by_kind"].items()):
        out.append(f"  {kind}: {n_done} of {n_open} locked")
    for r in lk["done"]:
        legs = ", ".join(f"{s} {float(q):g}" for s, q in (r.get("legs") or {}).items())
        out.append(f"  locked {r.get('ticker')} {float(r.get('count') or 0):g} at "
                   f"{float(r.get('edge_c') or 0):+.1f}c a contract ({r.get('lock')}: {legs})")
    for r in h["done"]:
        out.append(f"  locked lead {r.get('job')}: {float(r.get('count') or 0):g} at "
                   f"{float(r.get('edge_c') or 0):+.1f}c a contract")
    g = ctx["groups"]
    out += ["", "COUNTS: complement " + f"{T['comp']['sides']} sides led "
                f"({T['comp']['at_end']} at the day's end)"
                + (f", {g[0]} mutually exclusive events read ({g[1]} two-market)" if g else "")
                + f"; fair {T['fair']['sides']} sides ({T['fair']['at_end']} at the end); "
                f"lead lock jobs {len(h['opened'])} opened, {len(h['sends'])} maker legs rested, "
                f"{len(h['leg_fills'])} leg fill(s), {len(h['refused'])} refused."]

    # every lead, for reference: one line an event (a lone market's in full),
    # the ones that led a while or earned
    for label, note, group in (("COMPLEMENT", "", comp),
                               ("FAIR", f", at least {imm.FAIR_LEAD_MARGIN_C}c inside the fair",
                                fair)):
        if not group:
            continue
        keep = {k: v for k, v in group.items()
                if sum(e - s for s, e in v["spans"]) / 3600.0 >= LIST_MIN_HOURS
                or rew.get(k[0], 0.0) >= LIST_MIN_USD}
        dropped = len(group) - len(keep)
        by_ev: dict = defaultdict(list)
        for (t, side), v in keep.items():
            by_ev["-".join(t.split("-")[:-1]) or t].append((t, side, v))
        out += ["", f"ALL {label} LEADS by event{note} (led {LIST_MIN_HOURS * 60:.0f} min+ "
                    f"or earned ${LIST_MIN_USD:.2f}+"
                    + (f"; {dropped} shorter one{'s' if dropped != 1 else ''} left out" if dropped
                       else "") + ")"]
        for ev, items in sorted(by_ev.items(), key=lambda kv: -sum(rew.get(t, 0.0) for t in
                                                                   {x[0] for x in kv[1]})):
            if len(items) == 1:
                t, side, v = items[0]
                out.append(f"  {_lead_line(t, side, v, rew.get(t))}")
                continue
            ts_ = {t for t, _s, _v in items}
            sides = defaultdict(int)
            for _t, side, _v in items:
                sides[side] += 1
            hrs = [sum(e - s for s, e in v["spans"]) / 3600.0 for _t, _s, v in items]
            end = sum(1 for _t, _s, v in items if v["leading_at_end"])
            out.append(f"  {ev}: {', '.join(f'{n} {s}' + ('s' if n != 1 else '') for s, n in sorted(sides.items()))} on "
                       f"{len(ts_)} markets, led {min(hrs):.1f}-{max(hrs):.1f}h, "
                       f"${sum(rew.get(t, 0.0) for t in ts_):.2f} while leading"
                       + (f"; {end} still leading" if end else "; all ended"))
    out += ["", "Switches (run_incentive_mm.ps1, then restart_imm.ps1 -Task): "
                "IMM_COMP_LEAD=dry|off stops placing leads; IMM_COMP_HEDGE=dry|off stops "
                "the lead fills' hedge orders; IMM_LOCK=dry|off the other fills' locks; "
                "IMM_FAIR_LEAD=dry|off the fair leads."]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker")
    ap.add_argument("--day", help="ET day to report (YYYY-MM-DD); default yesterday")
    args = ap.parse_args(argv)
    low_priority()

    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else today_et - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_comp_lead_monitor_sent_{today_et}.marker")
    if not (args.test or args.dry or args.day) and os.path.exists(marker):
        log(f"complement lead email already sent for {today_et}; exiting")
        return 0
    ctx = build_report(day, now=now_utc)
    text, subject = render_text(ctx), subject_for(ctx)
    log("complement lead email: " + subject + "\n" + text)
    if args.dry:
        return 0
    attempts = 1 if args.test else 8
    alerter = imm.Alerter("IMM-COMPLEAD", live=True)
    if not alerter.enabled:
        log("cannot send complement lead email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject)
        if ok:
            break
        log(f"complement lead send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"complement lead send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.day):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR, "imm_comp_lead_monitor_sent_*.marker")):
            name = os.path.basename(old)[len("imm_comp_lead_monitor_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
