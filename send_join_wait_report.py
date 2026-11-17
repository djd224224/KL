#!/usr/bin/env python3
r"""send_join_wait_report.py -- the daily join-wait email (Jack 2026-11-02:
"give me daily email report on its performance").

THE JOIN WAIT (a62ee6c, live 21:13 ET 11/02; incentive_mm JOIN_WAIT_MODE): an
at-ref rung joins a better price only once that price has stood 2 s per $ at
risk over the side's 30-min anchor, the first $50 free; a worse price at once.
Built after the 11/02 Paris rain bait (-$400). The 11/02 replay: ~$1.36/day of
reward given up, and it blocks the two Paris fills and nothing else.

ONE EMAIL per ET day, ending in OK / WATCH / PROBLEM:
  waits     how often a side waited and for how long, and how each wait ended:
            JOINED (the better price stood its wait) or WENT AWAY (it vanished
            first -- the shape a bait leaves)
  kept out  every public Kalshi trade through a waiting side's wanted price
            that was not one of our fills: a fill the wait kept us out of,
            capped at the side's size, marked to the current mid (+ = a loss
            it saved us, - = a profit it cost us)
  better    our own fills on a waiting side, at the price it rested at: how
            much better they were than the wanted price
  reward    what the waits gave up: on every managed book snapshot of a
            waiting side, our top rung moved to the wanted price and scored
            with the bot's own share model against the book as it was
  net       kept out + better - reward, for the day, the last 7 days (the
            PROBLEM bar) and running since launch

THE SCORING FIXES (Jack 2026-11-17: "Yes, fix the report", to "Fix the
join-wait report's scoring?"). The 11/16 email read PROBLEM on a quiet day
(-$2.39) because:
  - the PROBLEM bar was the net SINCE LAUNCH. Election night alone (11/03,
    -$156) kept it under -$50 every day from 11/09. It is now the net of the
    last 7 days, so a bad stretch clears after a week.
  - a day's tape read cut short by its time budget stayed in the store as it
    was. 11/14 was stored at -$203. Its read stopped partway through the
    alphabet, past one -$143 AUDUSD miss and before a +$229 KXLOWTPERIOD save;
    read in full on 11/17 it came to +$93. Each run now reads again any stored
    day of the last RESCORE_MAX_AGE_DAYS whose read did not finish (up to
    RESCORE_MAX_DAYS a run, newest first) and keeps the new numbers once that
    read finishes.
  - the read went through markets in ticker order. It now starts with the
    markets that had the most $ at risk on their waits.

SOURCES, all read-only: the bot's join_wait_<UTC date>.jsonl sink and day logs
(the hold lines' numbers, the startup mode line, "join wait failed" errors),
the book_depth snapshots, fills_<UTC date>.jsonl, and Kalshi's PUBLIC
/markets and /markets/trades (no auth, paced: send_lone_ref_monitor's
readers). Writes only the sent-marker and join_wait_report.json (each day's
result, for the running totals). Imports imm_quote_gaps first, like the
other morning emails, so the launcher env and the alert credentials apply.
Idle priority: the bot shares the PC.

USAGE:
  python send_join_wait_report.py                   # the scheduled daily run
  python send_join_wait_report.py --dry             # build + print, send nothing
  python send_join_wait_report.py --test            # send now, ignore the marker
  python send_join_wait_report.py --day 2026-11-03  # a specific ET day
  python send_join_wait_report.py --no-tape         # skip the Kalshi reads
  python send_join_wait_report.py --no-rescore      # skip re-reading cut-short days
Scheduled daily as "KL imm join-wait" (register_imm_join_wait.ps1), 08:20 local.
Sends every morning, so a quiet day is distinguishable from a broken task.
All console output is ASCII (the task console is cp1252).
"""
import argparse
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
import send_lone_ref_monitor as slr  # noqa: E402  (window, book, tape and mid readers)
from incentive_mm import log  # noqa: E402

LIVE_TS = datetime(2026, 11, 3, 2, 13, 1, tzinfo=timezone.utc).timestamp()
REPLAY_COST_PER_DAY = 1.36      # the 11/02 replay's reward given up, $/day
DT_CAP_SECS = 180.0             # a book snapshot stands for at most this long
OWN_FILL_MATCH_SECS = 3.0       # a public trade this near one of our fills on the side is it
TAPE_BUDGET_SECS = 900
# verdict bars, $ a full day (a partial day scales the reward bars)
COST_WATCH = 5.0                # ~3.5x the replay's reward cost
COST_PROBLEM = 15.0             # ~10x
KEPT_OUT_GOOD_WATCH = 25.0      # profits the waits kept us out of
DAY_NET_WATCH = -10.0
WEEK_NET_PROBLEM = -50.0        # the net of the last WEEK_DAYS stored days
WEEK_DAYS = 7
RESCORE_MAX_AGE_DAYS = 14       # re-read a cut-short day's tape for this long
RESCORE_MAX_DAYS = 2            # at most this many re-reads a run (each up to TAPE_BUDGET_SECS)
LONG_WAIT_WATCH_SECS = 2 * 3600  # one side behind a better price this long
FLAP_WATCH = 30                 # one side waiting this many times in a day
LEVELS = ("OK", "WATCH", "PROBLEM")

_HOLD = re.compile(
    r"^(\S+ \S+)Z \[IMM\] join wait(?: \(dry\))? (\S+) (bid|ask): wants (\d+)c, rests "
    r"(\d+)c -- ([\d.e+]+) contracts x (-?\d+)c over its [\d.]+-min anchor (\d+)c = "
    r"\$([\d,]+) at risk, the first \$[\d.]+ free: \d+c must stand ([\d,]+)s")
_MODE = re.compile(r"^(\S+ \S+)Z join wait \(2026-11-02\): (\w+)")
_FAIL = re.compile(r"^(\S+ \S+)Z \[IMM\] ! join wait failed")


def store_path() -> str:
    """Resolved at call time, so a sandboxed STATUS_DIR (tests) is honoured."""
    return os.path.join(imm.STATUS_DIR, "join_wait_report.json")


def money(x: float) -> str:
    """Signed dollars: cents under $10 (the reward cost is ~$1 a day)."""
    if abs(x) < 0.005:
        return "$0.00"
    return ("-$" if x < 0 else "+$") + (f"{abs(x):,.2f}" if abs(x) < 10 else f"{abs(x):,.0f}")


def dur(secs: float) -> str:
    secs = max(0.0, float(secs))
    if secs < 120:
        return f"{secs:.0f}s"
    if secs < 7200:
        return f"{secs / 60:.0f} min"
    return f"{secs / 3600:.1f} h"


# ---- the waits -----------------------------------------------------------------

def sink_rows(a_ts: float, b_ts: float) -> list:
    """The join-wait sink's rows from a day before a_ts up to b_ts, oldest first
    (a wait can begin before the window)."""
    out = []
    for d in slr.utc_dates(a_ts - 86400, b_ts):
        try:
            with open(os.path.join(imm.STATUS_DIR, f"join_wait_{d}.jsonl"),
                      encoding="utf-8") as fh:
                for ln in fh:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue
                    if a_ts - 86400 <= float(r.get("ts") or 0) < b_ts:
                        out.append(r)
        except OSError:
            continue
    return sorted(out, key=lambda r: float(r["ts"]))


def episodes(rows, a_ts: float, b_ts: float) -> list:
    """Waits from the sink rows in time order: a "hold" opens one per side and
    its "join" or "gone" closes it; one still open at b_ts ends there
    ("waiting"). Clipped to [a_ts, b_ts); a wait wholly outside is dropped.
    want / rest are YES cents at the hold, size its contracts."""
    open_: dict = {}
    eps: list = []

    def close(key, t, how):
        e = open_.pop(key)
        if t > a_ts and e["start"] < b_ts:
            e["start"], e["end"], e["how"] = max(e["start"], a_ts), min(t, b_ts), how
            e["dur"] = e["end"] - e["start"]
            eps.append(e)

    for r in rows:
        key = (r.get("ticker"), r.get("side"))
        t = float(r["ts"])
        ev = r.get("event")
        if ev == "hold" and key not in open_ and t < b_ts:
            open_[key] = dict(ticker=key[0], side=key[1], start=t, t0=t,
                              want=int(r["want"]), rest=int(r["rest"]),
                              size=float(r.get("size") or 0.0), dry=r.get("mode") != "on")
        elif ev in ("join", "gone") and key in open_:
            close(key, t, ev)
    for key in list(open_):
        close(key, b_ts, "waiting")
    return eps


def read_logs(a_ts: float, b_ts: float) -> tuple:
    """(hold-line details by (ticker, side) -> [(ts, dict)], the last startup
    mode before b_ts, "join wait failed" lines inside the window) from the
    day logs."""
    det = defaultdict(list)
    mode, fails = None, 0
    for line in slr.day_log_lines(a_ts, b_ts):
        if "join wait" not in line:
            continue
        m = _HOLD.match(line)
        if m:
            det[(m.group(2), m.group(3))].append((slr._ts(m.group(1)), dict(
                anchor=int(m.group(8)), at_risk=float(m.group(9).replace(",", "")),
                need=float(m.group(10).replace(",", "")))))
            continue
        m = _MODE.match(line)
        if m and slr._ts(m.group(1)) < b_ts:
            mode = m.group(2)
            continue
        m = _FAIL.match(line)
        if m and a_ts <= slr._ts(m.group(1)) < b_ts:
            fails += 1
    return det, mode, fails


def attach_details(eps: list, det: dict) -> None:
    """Each wait's anchor, $ at risk and required stand from its hold line
    (the gap to where it rested when the line is not found)."""
    for e in eps:
        got = [d for ts, d in det.get((e["ticker"], e["side"]), ()) if abs(ts - e["t0"]) <= 2.0]
        if got:
            e.update(got[0])
        else:
            e.setdefault("at_risk", e["size"] * abs(e["want"] - e["rest"]) / 100.0)
            e.setdefault("need", None)


# ---- reward given up, from the book snapshots ---------------------------------

def _top(own_side: dict):
    """Our top rung's price on one side (own price space), pads out."""
    px = [p for p in own_side if p > imm.PAD_BID_CENTS]
    return max(px) if px else None


def _lv(d: dict) -> list:
    return sorted(((p, q) for p, q in d.items() if q > 1e-9), key=lambda kv: -kv[0])


def wait_rate(full: dict, own: dict, sd: str, want_own: int, target: float, df: float,
              pool: float) -> float:
    """$/day one waiting side gives up on one book snapshot: our top rung moved
    from where it rests to the wanted price (own price space), scored with the
    bot's share model against the book as it was. 0 when it rests there
    already, or has no rung."""
    top = _top(own[sd])
    if top is None or want_own <= top or target <= 0:
        return 0.0
    other = "no" if sd == "yes" else "yes"
    s_ot, q_ot = imm._side_share(_lv(full[other]), own[other], target, df)
    s_ac, q_ac = imm._side_share(_lv(full[sd]), own[sd], target, df)
    cf, cfo = dict(full[sd]), dict(own[sd])
    sz = cfo.pop(top)
    cf[top] = cf.get(top, 0.0) - sz
    cf[want_own] = cf.get(want_own, 0.0) + sz
    cfo[want_own] = cfo.get(want_own, 0.0) + sz
    s_cf, q_cf = imm._side_share(_lv(cf), cfo, target, df)
    f_ac = (s_ac + s_ot) / 2.0 if (q_ac and q_ot) else 0.0
    f_cf = (s_cf + s_ot) / 2.0 if (q_cf and q_ot) else 0.0
    return max(0.0, f_cf - f_ac) * pool


def score_waits(eps: list, a_ts: float, b_ts: float, rows=None) -> None:
    """Add each wait's reward cost ($) from the managed book snapshots of
    [a_ts, b_ts). `rows` (tests) replaces the book_depth files with
    (ts, row-dict) pairs in time order."""
    by_key = defaultdict(list)
    for e in eps:
        e.setdefault("cost", 0.0)
        by_key[(e["ticker"], "yes" if e["side"] == "bid" else "no")].append(e)
    if not eps:
        return
    if rows is None:
        rows = slr._book_rows({e["ticker"] for e in eps}, a_ts, b_ts)
    prev: dict = {}
    for ts, r in rows:
        t = r["ticker"]
        p = prev.get(t)
        if p:
            dt = max(0.0, min(ts - p[0], DT_CAP_SECS))
            for e, rate in p[1]:
                e["cost"] += rate * dt / 86400.0
        target = float(r.get("target") or 0.0)
        df = float(r.get("discount") or 0.5)
        pool = float(r.get("pool_per_day") or 0.0)
        dollars = r.get("unit", "dollars") == "dollars"
        full = {"yes": slr._levels(r.get("yes"), dollars), "no": slr._levels(r.get("no"), dollars)}
        own = {"yes": slr._own(r.get("own_yes_cents")), "no": slr._own(r.get("own_no_cents"))}
        rates = []
        for sd in ("yes", "no"):
            for e in by_key.get((t, sd), ()):
                if e["start"] <= ts < e["end"]:
                    want_own = e["want"] if sd == "yes" else 100 - e["want"]
                    rates.append((e, wait_rate(full, own, sd, want_own, target, df, pool)))
                    break
        prev[t] = (ts, rates)


# ---- fills: the ones it kept us out of, and our own at a better price ---------

def classify_trades(eps: list, trades: list, ours: dict, mid) -> list:
    """(wait, contracts, $ that fill would have made vs `mid`) for each public
    trade through a waiting side's wanted price while it waited: a taker sale
    at or under a waiting bid's wanted price, a taker buy at or over a waiting
    ask's (YES cents). A trade within OWN_FILL_MATCH_SECS of one of our own
    fills on that side is that fill (see better_fills). Contracts are capped
    at the wait's size, spent as the trades come."""
    left = {id(e): e["size"] for e in eps}
    out = []
    for tr in sorted(trades, key=lambda x: x["created_time"]):
        tts = datetime.fromisoformat(tr["created_time"].replace("Z", "+00:00")).timestamp()
        yes = float(tr["yes_price_dollars"]) * 100
        for e in eps:
            if not (e["start"] <= tts < e["end"]) or left[id(e)] <= 0:
                continue
            if any(abs(tts - x) <= OWN_FILL_MATCH_SECS
                   for x in ours.get((e["ticker"], e["side"]), ())):
                continue
            if e["side"] == "bid" and tr.get("taker_side") == "no" and yes <= e["want"] + 1e-6:
                pnl_c = (mid - e["want"]) if mid is not None else 0.0
            elif e["side"] == "ask" and tr.get("taker_side") == "yes" and yes >= e["want"] - 1e-6:
                pnl_c = (e["want"] - mid) if mid is not None else 0.0
            else:
                continue
            n = min(float(tr.get("count_fp") or tr.get("count") or 0.0), left[id(e)])
            left[id(e)] -= n
            out.append((e, n, pnl_c * n / 100.0))
            break
    return out


def _trades(t: str, a: float, b: float) -> list:
    trades, cur = [], ""
    while True:
        params = {"ticker": t, "min_ts": int(a), "max_ts": int(b) + 1, "limit": 1000}
        if cur:
            params["cursor"] = cur
        r = slr._get("/markets/trades", params)
        trades += r.get("trades") or []
        cur = r.get("cursor") or ""
        if not cur:
            return trades


def at_risk(e: dict) -> float:
    """A wait's $ at risk: its hold line's, else size x the gap to where it
    rested (a re-read skips the day logs)."""
    v = e.get("at_risk")
    return float(v) if v is not None else e["size"] * abs(e["want"] - e["rest"]) / 100.0


def kept_out(eps: list, fills: list, mids: dict) -> dict:
    """The fills the waits kept us out of, from one public trades read per
    market, marked to the current mid. saved = -(what they would have made).
    The markets with the most $ at risk on their waits are read first, so a
    read cut short by its time budget misses the smallest ones."""
    by_t = defaultdict(list)
    for e in eps:
        by_t[e["ticker"]].append(e)
    ours = defaultdict(list)
    for f in fills:
        ours[(f["ticker"], f["our_book_side"])].append(float(f["ts"]))
    res = dict(trades=0, contracts=0.0, saved=0.0, good=0.0, top=[], done=True)
    stop = time.time() + TAPE_BUDGET_SECS
    order = sorted(by_t.items(), key=lambda kv: (-sum(at_risk(e) for e in kv[1]), kv[0]))
    for t, es in order:
        if time.time() > stop:
            res["done"] = False
            break
        try:
            trades = _trades(t, min(e["start"] for e in es), max(e["end"] for e in es))
            got = classify_trades(es, trades, ours, slr.mid_now(t, mids)) if trades else []
        except Exception as ex:                      # one market never costs the email
            log(f"join-wait report: tape read failed for {t}: {ex!r}")
            continue
        for e, n, made in got:
            res["trades"] += 1
            res["contracts"] += n
            res["saved"] -= made
            res["good"] += max(0.0, made)
            e["kept"] = e.get("kept", 0.0) - made
            res["top"].append((-made, t, e["side"], e["want"], n))
    res["top"].sort(key=lambda x: -abs(x[0]))
    return res


def better_fills(eps: list, fills: list) -> dict:
    """Our maker fills on a waiting side during its wait, at its resting price:
    (wanted - paid) on a bid, (sold - wanted) on an ask, x contracts."""
    by_key = defaultdict(list)
    for e in eps:
        by_key[(e["ticker"], e["side"])].append(e)
    res = dict(fills=0, contracts=0.0, usd=0.0, top=[])
    for f in fills:
        side = f["our_book_side"]
        e = next((e for e in by_key.get((f["ticker"], side), ())
                  if e["start"] <= f["ts"] < e["end"]), None)
        if e is None:
            continue
        px = float(f.get("our_price_cents") or f.get("yes_price_cents") or 0.0)
        n = float(f.get("count") or 0.0)
        gain = ((e["want"] - px) if side == "bid" else (px - e["want"])) * n / 100.0
        res["fills"] += 1
        res["contracts"] += n
        res["usd"] += gain
        e["better"] = e.get("better", 0.0) + gain
        res["top"].append((gain, f["ticker"], side, px, n))
    return res


# ---- the report ------------------------------------------------------------------

def load_store() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_store(data: dict) -> None:
    tmp = store_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, store_path())


def rescore_cut_short(store: dict, skip_day: str, now: datetime) -> list:
    """Read the public trades again for each stored day whose read did not
    finish (cut short by its time budget, or never run), at most
    RESCORE_MAX_DAYS of them, newest first, from the last RESCORE_MAX_AGE_DAYS.
    Once a read finishes, the day takes its kept-out numbers and its net;
    reward and better fills stay as stored (the book snapshots behind the
    reward may be gone by now). Returns (day, old net, new net, finished) for
    each day it read, and updates `store` in place."""
    oldest = (now.astimezone(imm.ET).date() - timedelta(days=RESCORE_MAX_AGE_DAYS)).isoformat()
    due = sorted((k for k, v in store.items()
                  if k != skip_day and k >= oldest
                  and not (v.get("tape_read") and v.get("tape_done"))), reverse=True)
    out = []
    for k in due[:RESCORE_MAX_DAYS]:
        v = store[k]
        a_ts, b_ts = float(v["a_ts"]), float(v["b_ts"])
        eps = episodes(sink_rows(a_ts, b_ts), a_ts, b_ts)
        kept = kept_out(eps, slr.maker_fills(a_ts, b_ts), {}) if eps else \
            dict(trades=0, contracts=0.0, saved=0.0, good=0.0, done=True)
        old = float(v.get("net", 0.0))
        if kept["done"]:
            v.update(saved=kept["saved"], good=kept["good"], kept_trades=kept["trades"],
                     kept_contracts=kept["contracts"], tape_read=True, tape_done=True,
                     rescored=now.isoformat())
            v["net"] = v["saved"] + v.get("better", 0.0) - v.get("cost", 0.0)
        log(f"join-wait report: re-read {k}'s public trades "
            f"({'finished' if kept['done'] else 'cut short again; kept as stored'}): "
            f"net {money(old)} -> {money(v['net'])}")
        out.append((k, old, float(v["net"]), bool(kept["done"])))
    return out


def window(store: dict, day: str, n: int = WEEK_DAYS) -> dict:
    """The stored days in the n days ending on `day`: their net and count."""
    first = (datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=n - 1)).isoformat()
    ks = sorted(k for k in store if first <= k <= day)
    return dict(n=n, days=len(ks), first=ks[0] if ks else first, last=day,
                net=sum(store[k]["net"] for k in ks),
                saved=sum(store[k]["saved"] for k in ks),
                better=sum(store[k]["better"] for k in ks),
                cost=sum(store[k]["cost"] for k in ks))


def verdict(d: dict, run: dict) -> tuple:
    """(level, reasons): PROBLEM / WATCH / OK for one day and the running
    totals; the reward bars scale with the share of the day it covered."""
    reasons, level = [], 0

    def flag(lv, why):
        nonlocal level
        level = max(level, lv)
        reasons.append(why)

    frac = max(d["hours"] / 24.0, 1e-9)
    if d["mode"] != "on":
        flag(2 if d["mode"] in (None, "off") else 1,
             f"the bot's startup line reads join wait {d['mode'] or 'unknown'}, not on")
    if d["errors"]:
        flag(2, f"{d['errors']} 'join wait failed' line(s) in the bot's log")
    if d["cost"] >= COST_PROBLEM * frac:
        flag(2, f"reward given up {money(-d['cost'])} (bar {money(-COST_PROBLEM * frac)}; "
                f"the replay expected {money(-REPLAY_COST_PER_DAY * frac)})")
    elif d["cost"] >= COST_WATCH * frac:
        flag(1, f"reward given up {money(-d['cost'])} (bar {money(-COST_WATCH * frac)}; "
                f"the replay expected {money(-REPLAY_COST_PER_DAY * frac)})")
    if d["good"] >= KEPT_OUT_GOOD_WATCH:
        flag(1, f"it kept us out of fills that would have made {money(d['good'])}")
    if d["net"] <= DAY_NET_WATCH:
        flag(1, f"the day's net {money(d['net'])}: the waits cost more than they saved")
    if d["max_s"] >= LONG_WAIT_WATCH_SECS:
        flag(1, f"{d['max_side']} waited {dur(d['max_s'])} behind a better price")
    if d["max_flaps"] >= FLAP_WATCH:
        flag(1, f"{d['max_flaps_side']} waited {d['max_flaps']} times (flapping)")
    week = run.get("week") or {}
    if week.get("days") and week["net"] <= WEEK_NET_PROBLEM:
        flag(2, f"the last {week['n']} days' net {money(week['net'])} "
                f"({week['first']} to {week['last']}; bar {money(WEEK_NET_PROBLEM)})")
    return LEVELS[level], reasons


def build_report(day, with_tape: bool = True, now=None, rows=None, rescore: bool = True) -> dict:
    now = now or datetime.now(timezone.utc)
    a_ts, b_ts = slr.day_window(day)
    a_ts = max(a_ts, LIVE_TS)
    b_ts = max(a_ts, min(b_ts, now.timestamp()))
    eps = episodes(sink_rows(a_ts, b_ts), a_ts, b_ts)
    det, mode, fails = read_logs(a_ts, b_ts)
    if mode is None:
        _d, mode, _f = read_logs(a_ts - 7 * 86400, b_ts)
    attach_details(eps, det)
    score_waits(eps, a_ts, b_ts, rows=rows)
    fills = slr.maker_fills(a_ts, b_ts)
    better = better_fills(eps, fills)
    empty = dict(trades=0, contracts=0.0, saved=0.0, good=0.0, top=[], done=True)
    kept = kept_out(eps, fills, {}) if with_tape and eps else empty
    flaps = defaultdict(int)
    for e in eps:
        flaps[(e["ticker"], e["side"])] += 1
    worst = max(flaps.items(), key=lambda kv: kv[1]) if flaps else (("", ""), 0)
    durs = sorted(e["dur"] for e in eps)
    longest = max(eps, key=lambda e: e["dur"]) if eps else None
    d = dict(day=day.isoformat(), a_ts=a_ts, b_ts=b_ts, hours=(b_ts - a_ts) / 3600.0,
             mode=mode, errors=fails, waits=len(eps), sides=len(flaps),
             markets=len({e["ticker"] for e in eps}),
             joined=sum(1 for e in eps if e["how"] == "join"),
             gone=sum(1 for e in eps if e["how"] == "gone"),
             waiting=sum(1 for e in eps if e["how"] == "waiting"),
             median_s=durs[len(durs) // 2] if durs else 0.0,
             max_s=longest["dur"] if longest else 0.0,
             max_side=f"{longest['ticker']} {longest['side']}" if longest else "",
             max_flaps=worst[1], max_flaps_side=" ".join(worst[0]).strip(),
             cost=sum(e["cost"] for e in eps), saved=kept["saved"], good=kept["good"],
             kept_trades=kept["trades"], kept_contracts=kept["contracts"],
             better=better["usd"], better_fills=better["fills"],
             tape_done=kept["done"], tape_read=bool(with_tape))
    d["net"] = d["saved"] + d["better"] - d["cost"]
    store = load_store()
    rescored = rescore_cut_short(store, d["day"], now) if (with_tape and rescore) else []
    store[d["day"]] = d
    run = dict(days=sum(v["hours"] for v in store.values()) / 24.0,
               net=sum(v["net"] for v in store.values()),
               cost=sum(v["cost"] for v in store.values()),
               saved=sum(v["saved"] for v in store.values()),
               better=sum(v["better"] for v in store.values()),
               waits=sum(v["waits"] for v in store.values()),
               gone=sum(v["gone"] for v in store.values()),
               week=window(store, d["day"]))
    level, reasons = verdict(d, run)
    d["level"] = level
    top = sorted(eps, key=lambda e: -e.get("at_risk", 0.0))
    return dict(d=d, run=run, level=level, reasons=reasons, store=store, top=top,
                kept_top=kept["top"][:6], better_top=sorted(better["top"], reverse=True)[:4],
                rescored=rescored)


def subject_for(ctx) -> str:
    d = ctx["d"]
    day = datetime.strptime(d["day"], "%Y-%m-%d")
    dry = " (dry)" if d.get("mode") == "dry" else ""
    if not d["waits"]:
        return f"IMM join wait {day:%m/%d}{dry}: no waits | {ctx['level']}"
    return (f"IMM join wait {day:%m/%d}{dry}: net {money(d['net'])} | {d['waits']} wait"
            f"{'s' if d['waits'] != 1 else ''}, {d['gone']} went away | {ctx['level']}")


def _et(ts: float) -> str:
    return f"{datetime.fromtimestamp(ts, timezone.utc).astimezone(imm.ET):%H:%M}"


def render_text(ctx) -> str:
    d, run = ctx["d"], ctx["run"]
    a = datetime.fromtimestamp(d["a_ts"], timezone.utc).astimezone(imm.ET)
    b = datetime.fromtimestamp(d["b_ts"], timezone.utc).astimezone(imm.ET)
    exp = REPLAY_COST_PER_DAY * d["hours"] / 24.0
    tape = ("" if d["tape_read"] and d["tape_done"] else
            " (public trades not read; the next run reads them)" if not d["tape_read"] else
            " (public trades read cut short by its time budget; the next run reads them "
            "again)")
    if d.get("mode") == "dry":
        head = (f"{ctx['level']}: the join wait is DRY -- nothing was held; these are the "
                f"waits it would have made. ")
    else:
        head = f"{ctx['level']}: "
    if not d["waits"]:
        lines = [head + "no waits today: no at-ref rung had more than the free $50 at risk "
                        "on a better price."]
    else:
        kept = (f"The waits kept us out of {d['kept_trades']} trade"
                f"{'s' if d['kept_trades'] != 1 else ''} through a price we waited on -- those "
                f"fills would have {'lost' if d['saved'] >= 0 else 'made'} "
                f"${abs(d['saved']):,.2f} by now{tape}" if d["kept_trades"] else
                f"No public trade went through a price we waited on{tape}")
        better = (f"; {d['better_fills']} of our own fills came at a better price, worth "
                  f"{money(d['better'])}" if d["better_fills"] else "")
        lines = [
            head + f"net {money(d['net'])} today. {kept}{better}; the waits gave up "
            f"${d['cost']:,.2f} of reward (the replay expected about ${exp:,.2f} for this "
            f"stretch).",
            f"Waits: {d['waits']} on {d['sides']} side{'s' if d['sides'] != 1 else ''} of "
            f"{d['markets']} market{'s' if d['markets'] != 1 else ''}. {d['joined']} joined after "
            f"waiting, {d['gone']} went away first (the shape a bait leaves), {d['waiting']} "
            f"still waiting at the day's end. Median {dur(d['median_s'])}, longest "
            f"{dur(d['max_s'])} ({d['max_side']}).",
        ]
    wk = run.get("week")
    if wk and wk["days"]:
        lines.append(
            f"Last {wk['n']} days ({datetime.strptime(wk['first'], '%Y-%m-%d'):%m/%d}-"
            f"{datetime.strptime(wk['last'], '%Y-%m-%d'):%m/%d}, the PROBLEM bar "
            f"{money(WEEK_NET_PROBLEM)}): net {money(wk['net'])} -- kept out "
            f"{money(wk['saved'])}, better fills {money(wk['better'])}, reward "
            f"{money(-wk['cost'])}.")
    for k, old, new, done in ctx.get("rescored") or ():
        lines.append(
            f"Re-read {datetime.strptime(k, '%Y-%m-%d'):%m/%d}'s public trades (its first read "
            f"did not finish): " + (f"net {money(old)} -> {money(new)}." if done else
                                    f"cut short again; kept at {money(old)}."))
    since = datetime.fromtimestamp(LIVE_TS, timezone.utc).astimezone(imm.ET)
    lines.append(
        f"Running since {since:%m/%d %H:%M} ET: net {money(run['net'])} over "
        f"{run['days']:.1f} days -- kept out {money(run['saved'])}, better fills "
        f"{money(run['better'])}, reward {money(-run['cost'])}; {run['waits']} waits, "
        f"{run['gone']} went away.")
    if ctx["top"]:
        lines += ["", "The waits, biggest at risk first:"]
        for e in ctx["top"][:8]:
            how = {"join": "joined", "gone": "went away", "waiting": "still waiting"}[e["how"]]
            need = f", must stand {dur(e['need'])}" if e.get("need") is not None else ""
            extra = ""
            if e.get("kept"):
                extra += f"; kept out {money(e['kept'])}"
            if e.get("better"):
                extra += f"; better fills {money(e['better'])}"
            lines.append(
                f"  {_et(e['t0'])} ET {e['ticker']} {e['side']}: wanted {e['want']}c, rested "
                f"{e['rest']}c; {e['size']:,.0f} contracts, ${e.get('at_risk', 0.0):,.0f} at risk"
                f"{need} -> {how} after {dur(e['dur'])}; reward {money(-e['cost'])}{extra}")
    if ctx["kept_top"]:
        lines += ["", "Trades it kept us out of (+ = the loss it saved, marked to now):"]
        for saved, t, side, want, n in ctx["kept_top"]:
            lines.append(f"  {t} {side} {n:,.0f} @ {want}c: {money(saved)}")
    lines += ["", f"Window {a:%m/%d %H:%M}-{b:%H:%M} ET. Mode: {d['mode'] or 'unknown'}."]
    if ctx["reasons"]:
        lines += ["", "NEEDS A LOOK:"] + [f"- {r}" for r in ctx["reasons"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker, no store")
    ap.add_argument("--day", help="ET day to report (YYYY-MM-DD); default yesterday")
    ap.add_argument("--no-tape", action="store_true",
                    help="skip the Kalshi public reads (no kept-out fills)")
    ap.add_argument("--no-rescore", action="store_true",
                    help="do not re-read stored days whose public-trades read did not finish")
    args = ap.parse_args(argv)
    slr.low_priority()

    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else today_et - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_join_wait_report_sent_{today_et}.marker")
    if not (args.test or args.dry or args.day) and os.path.exists(marker):
        log(f"join-wait email already sent for {today_et}; exiting")
        return 0
    ctx = build_report(day, with_tape=not args.no_tape, now=now_utc,
                       rescore=not args.no_rescore)
    text, subject = render_text(ctx), subject_for(ctx)
    log("join-wait email: " + subject + "\n" + text)
    if args.dry:
        return 0
    try:
        save_store(ctx["store"])
    except OSError as e:
        log(f"! join_wait_report.json not written ({e!r})")
    alerter = imm.Alerter("IMM-JOINWAIT", live=True)
    if not alerter.enabled:
        log("cannot send join-wait email: alert credentials not configured")
        return 1
    attempts = 1 if args.test else 8
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=("[TEST] " if args.test else "") + subject)
        if ok:
            break
        log(f"join-wait send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"join-wait send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.day):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR, "imm_join_wait_report_sent_*.marker")):
            name = os.path.basename(old)[len("imm_join_wait_report_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
