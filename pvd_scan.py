#!/usr/bin/env python3
"""Post-vote dead-book SCANNER (Jack 2026-11-05: "make sure to be ready to
start quoting if these markets are relit overnight, or new similar markets are
launched overnight. same approach as today. quote markets based on ROI, be
careful to make sure they can surpass the $1 threshold, have an accompanying
dashboard").

Each run (Windows task "KL pvd-scan", every 15 min):
  1. reads Kalshi's active LIQUIDITY programs and keeps election-family markets
     (incentive_mm.election_series, not blocklisted) whose verified voting day
     has passed (election_cutoff_utc) -- the markets the family stands down --
     and that are either
       * RELIT: listed in post_vote_dead_books.json, with a program running
         past the listed stop (a new period: the $1 threshold counts from 0), or
       * NEW: not listed, on a program that started after NEW_SINCE (default
         2026-11-04T06:00Z, after the 05:40Z wave Jack's list was cut from --
         markets dropped from that wave by hand stay dropped until relit);
  2. re-reads each one's book (our own orders netted out) and last trade and
     scores it the way the 11/04 list was built:
       * kind: dead (touch 30c+ wide) or live;
       * decided side (last >= 90c -> the bid, <= 10c -> the ask): no cushion,
         2-99c; other sides 3-97c and a cushion off the last trade (dead 2c,
         live 5c) -- the bot's pvd_guard_quotes rules;
       * one rung a side at the book's reference; our share by the bot's
         estimate_reward_share; pool $/day from the program;
       * SIZE: full size from the start, to bank early and overnight while
         fills are few (Jack 2026-11-05): 120 a side on dead books, 80 on live
         books, 300 on a decided side. A market that cannot project BAR ($1.10)
         by its program end even at those sizes is NOT listed;
       * ROI = projected $/day / collateral if filled; added best ROI first
         while the scan-added collateral stays within BUDGET ($60k);
  3. writes the list atomically (the bot hot-loads it at its next 10-min
     universe refresh) and run-logs/pvd-scan/last_scan.json for the dashboard.
Listed markets inside their current period are never touched (the hand tuning
stands). Nothing is removed; every entry stops at its own program end.

    python pvd_scan.py            # dry run: prints what it would do
    python pvd_scan.py --write    # writes the list (the task runs this)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
LOG_DIR = os.path.join(HERE, "run-logs", "incentive-mm")
LIST = os.path.join(LOG_DIR, "post_vote_dead_books.json")
OUT_DIR = os.path.join(HERE, "run-logs", "pvd-scan")
os.makedirs(OUT_DIR, exist_ok=True)
LOCK = os.path.join(OUT_DIR, "scan.lock")
LAST = os.path.join(OUT_DIR, "last_scan.json")
PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"

# projected $ per program period. $1.10 = the bot's general payout bar (f0f0941, Jack
# 2026-11-05 "lower general bar to 1.1"); Kalshi has paid ~1.15x the estimate.
# Jack 2026-11-05 for overnight: "if you arent sure, bias towards quoting it" (was $1.30).
BAR = float(os.environ.get("PVD_SCAN_BAR", "1.10"))
# $ at risk if every scan-added rung fills, best ROI first. $60k (was $45k), leaning toward
# quoting overnight; a 24h what-if at full size had 1,161 markets clear $1.30 ($224k at
# risk): $45k took 473 (~$4.6k/day est), $60k 548 (~$5.2k/day), $75k 621 (~$5.6k/day).
BUDGET = float(os.environ.get("PVD_SCAN_BUDGET", "60000"))
# (Kalshi reserves no cash for resting orders -- each only has to be fundable
# when placed -- so this caps exposure, not cash. The 11/04 list rested ~$43k.)
NEW_SINCE = datetime.fromisoformat(os.environ.get("PVD_SCAN_NEW_SINCE", "2026-11-04T06:00:00+00:00"))
# DRY-RUN what-if only: project every candidate over this many hours (a fresh
# relit period) instead of its real time left. Ignored with --write.
SIM_HOURS = float(os.environ.get("PVD_SCAN_SIM_HOURS", "0") or 0)
# ANY-WAVE mode (the market-wave playbook, KL/.claude/skills/market-wave): set by
# flags; the defaults are the election behavior the "KL pvd-scan" task runs.
SERIES_PREFIXES = None   # --series KXFOO,KXBAR: membership by series prefix, not election_series
VOTE_CHECK = True        # --no-vote-check: don't require a verified voting day in the past
STOP_CAP = None          # --stop-cap ISO: no entry stops later than this (e.g. before a game / release)
REPORT = None            # --report PATH: every scored market + skip counts, JSON + CSV
GO_LIVE = "2026-11-04T13:49:12Z"   # the 11/04 list went live: entries without period_start date from it
DEAD_SIZE = int(os.environ.get("PVD_SCAN_DEAD_SIZE", "120"))       # a side, dead books
LIVE_SIZE = int(os.environ.get("PVD_SCAN_LIVE_SIZE", "80"))        # a side, live books
DECIDED_SIZE = int(os.environ.get("PVD_SCAN_DECIDED_SIZE", "300"))  # the decided side (winner's bid / loser's ask)
CUSH = {"dead": 2, "live": 5}

import incentive_mm as imm                      # noqa: E402  (reads only: membership, voting days, scoring)
import send_imm_new_programs as snp             # noqa: E402

_loc = threading.local()


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} {msg}"
    print(line, flush=True)
    with open(os.path.join(OUT_DIR, "scan.log"), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def pget(path: str, params=None):
    s = getattr(_loc, "s", None)
    if s is None:
        s = _loc.s = requests.Session()
    for a in range(8):
        try:
            r = s.get(PUBLIC + path, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 + 3 * a)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(1 + a)
    return None


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def zs(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def ref_px(levels: dict):
    acc = 0.0
    for px, q in sorted(levels.items(), key=lambda kv: -kv[0]):
        acc += q
        if acc >= 200:
            return px
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--series", help="comma-separated series PREFIXES (any-wave mode)")
    ap.add_argument("--no-vote-check", action="store_true",
                    help="skip the election past-the-vote requirement (needed outside elections)")
    ap.add_argument("--stop-cap", help="ISO time no entry may stop after (program end otherwise)")
    ap.add_argument("--new-since", help="ISO: unlisted markets count as new if their program started after this")
    ap.add_argument("--report", help="write the scored candidates to this .json (+ .csv)")
    a = ap.parse_args()
    global SERIES_PREFIXES, VOTE_CHECK, STOP_CAP, REPORT, NEW_SINCE
    if a.series:
        SERIES_PREFIXES = tuple(x.strip() for x in a.series.split(",") if x.strip())
    VOTE_CHECK = not a.no_vote_check
    if a.stop_cap:
        STOP_CAP = datetime.fromisoformat(a.stop_cap.replace("Z", "+00:00"))
    if a.new_since:
        NEW_SINCE = datetime.fromisoformat(a.new_since.replace("Z", "+00:00"))
    REPORT = a.report
    if os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) < 1500:
        log("another scan is running (lock) -- skipped")
        return 0
    open(LOCK, "w").close()
    try:
        if a.write:
            global SIM_HOURS
            SIM_HOURS = 0.0
        return scan(a.write)
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


def scan(write: bool) -> int:
    t0 = time.time()
    now = datetime.now(timezone.utc)
    summary = {"at": zs(now), "write": write, "bar": BAR, "budget": BUDGET,
               "mode": {"series": list(SERIES_PREFIXES) if SERIES_PREFIXES else "election",
                        "vote_check": VOTE_CHECK, "stop_cap": zs(STOP_CAP) if STOP_CAP else None}}
    client = imm._fair_reader_client()
    # 1. active liquidity programs: per market, the LATEST-ending period
    per = {}
    for p in snp.fetch_programs_raw(client):
        if p.get("incentive_type") != "liquidity" or not p.get("end_date") or not p.get("start_date"):
            continue
        en = ts(p["end_date"])
        if en <= now:
            continue
        per.setdefault(p.get("market_ticker") or "", []).append(p)
    progs = {}
    for t, ps in per.items():
        end = max(ts(p["end_date"]) for p in ps)
        cur_ps = [p for p in ps if ts(p["end_date"]) == end]
        st = min(ts(p["start_date"]) for p in cur_ps)
        days = max((end - st).total_seconds() / 86400, 1 / 24)
        progs[t] = {"pd": sum(p["period_reward"] / 10000 for p in cur_ps) / days, "start": st,
                    "end": min(end, STOP_CAP) if STOP_CAP else end,
                    "target": float(cur_ps[0].get("target_size_fp") or 1000),
                    "df": (cur_ps[0].get("discount_factor_bps") or 5000) / 10000}
    cur = json.load(open(LIST)) if os.path.exists(LIST) else {"markets": {}}
    L = cur.setdefault("markets", {})
    cands, series_ok = {}, {}
    n_listed_cur = n_old_wave = n_stopped = 0
    # markets Jack stopped by hand ("stopped": {ticker: {"at", "reason", "entry"}},
    # which the bot never reads): left alone until a program period starting
    # after the stop relights them
    stopped = cur.get("stopped") or {}
    for t, pr in progs.items():
        s = t.split("-")[0]
        if s not in series_ok:
            member = (any(s.startswith(x) for x in SERIES_PREFIXES) if SERIES_PREFIXES
                      else imm.election_series(s))
            series_ok[s] = member and not imm.series_blocklisted(s)
        if not series_ok[s]:
            continue
        e = L.get(t)
        hs = stopped.get(t)
        if SIM_HOURS:
            # what-if (dry run): every market relit on a fresh period -- listed,
            # stopped and old-wave alike are scored as relit/new
            pass
        elif hs is not None and pr["start"] <= ts(hs.get("at") or zs(now)):
            n_stopped += 1
            continue                        # stopped by hand for this period (a later period may relist)
        elif e is not None:
            if ts(e["stop"]) >= pr["end"]:
                n_listed_cur += 1
                continue                    # listed for this period: hands off
            pr["extend"] = pr["start"] <= ts(e.get("period_start") or GO_LIVE)
        elif pr["start"] < NEW_SINCE:
            n_old_wave += 1
            continue                        # the 11/04 wave, already judged by hand
        hrs = SIM_HOURS or (pr["end"] - max(now, pr["start"])).total_seconds() / 3600
        if hrs < 1 or pr["pd"] * hrs / 24 < BAR:
            continue                        # can't reach the bar even with the whole pool
        cands[t] = pr
    n_ext = sum(1 for t in cands if cands[t].get("extend"))
    n_relit = sum(1 for t in cands if t in L) - n_ext
    summary.update(programs=len(progs), listed_current=n_listed_cur, old_wave_skipped=n_old_wave,
                   stopped_by_hand=n_stopped,
                   candidates=len(cands), extend_candidates=n_ext, relit_candidates=n_relit,
                   new_candidates=len(cands) - n_relit - n_ext)
    log(f"programs {len(progs)} | election family: {n_listed_cur} listed for this period, {n_old_wave} from the "
        f"11/04 wave left as judged | candidates {len(cands)} (same program runs past the listed stop "
        f"{n_ext}, relit {n_relit}, new {len(cands) - n_relit - n_ext})")
    taken = []
    if cands:
        taken = score_and_pick(client, cands, L, now, summary)
    summary["taken"] = [{k: (zs(v) if isinstance(v, datetime) else v) for k, v in x.items()} for x in taken]
    if write and taken:
        stamp = now.strftime("%Y-%m-%dT%H:%MZ")
        for x in taken:
            old = L.get(x["t"]) or {}
            if x["ext"]:
                # the SAME program period runs on past the listed stop: keep the hand
                # tuning, move the stop, raise (never cut) a side the score wants bigger
                e = dict(old)
                e["stop"] = zs(x["end"])
                e["extended"] = f"scan {stamp}: stop {old.get('stop')} -> {zs(x['end'])}"
                bs0 = old.get("bid_size", old.get("size", 40))
                as0 = old.get("ask_size", old.get("size", 40))
                if x["bs"] > bs0:
                    e["bid_size"] = x["bs"]
                if x["as_"] > as0:
                    e["ask_size"] = x["as_"]
                L[x["t"]] = e
                continue
            e = {"event": x["ev"], "kind": x["kind"], "stop": zs(x["end"]), "size": x["size"],
                 "pool_day": round(x["pd"], 2), "last": x["last"], "book": x["book"],
                 "est_usd_day": round(x["rate"], 2), "proj_period": round(x["proj"], 2),
                 "coll_usd": round(x["coll"], 2), "cushion": x["c"], "period_start": zs(x["start"]),
                 "added": f"scan {stamp} ({'relit' if x['relit'] else 'new'}; ROI {x['roi'] * 100:.1f}%/day)"}
            if x["last"]:
                e["bid_max"], e["ask_min"] = math.floor(x["last"] - x["c"]), math.ceil(x["last"] + x["c"])
            else:
                e["bid_max"], e["ask_min"] = x["qb"], x["qa"]
            if x["bs"] != x["size"]:
                e["bid_size"] = x["bs"]
            if x["as_"] != x["size"]:
                e["ask_size"] = x["as_"]
            if old.get("no_losing_side"):
                e["no_losing_side"] = True              # Jack's per-market call (abc97d5) carries over
            if old:
                e["prev_period"] = {k: old.get(k) for k in ("stop", "size", "bid_size", "ask_size", "kind")
                                    if old.get(k) is not None}
            L[x["t"]] = e
        cur[f"scan_{stamp}"] = (f"pvd_scan.py: {len(taken)} ({sum(1 for x in taken if x['ext'])} extended, "
                                f"{sum(1 for x in taken if x['relit'])} relit) by ROI, "
                                f"bar ${BAR:.2f}, budget ${BUDGET:,.0f}")
        tmp = LIST + ".scan.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, indent=1, sort_keys=True)
        os.replace(tmp, LIST)
        log(f"WROTE {len(taken)} entries -> {LIST} (now {len(L)} listed)")
    elif not write:
        log("dry run: nothing written")
    summary["secs"] = round(time.time() - t0, 1)
    if write:
        with open(LAST + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=1)
        os.replace(LAST + ".tmp", LAST)
    return 0


def score_and_pick(client, cands, L, now, summary):
    mk = {}
    tick = sorted(cands)
    for i in range(0, len(tick), 100):
        j = pget("/markets", {"tickers": ",".join(tick[i:i + 100]), "limit": 1000}) or {}
        for m in j.get("markets") or []:
            mk[m["ticker"]] = m
        time.sleep(0.4)
    past = {}
    for t in tick:
        m = mk.get(t)
        if not m or m.get("status") not in ("active", "open"):
            continue
        if not VOTE_CHECK:
            past[t] = m                     # any-wave mode: the stop cap is the time gate
            continue
        try:
            cut = imm.election_cutoff_utc(m.get("event_ticker") or t.rsplit("-", 1)[0], m)
        except Exception:  # noqa: BLE001
            cut = None
        if cut is not None and cut <= now:
            past[t] = m
    summary["past_vote"] = len(past)
    log(f"past their voting day: {len(past)}")
    if not past:
        return []
    # our resting orders: netted out of the books; and the PVD collateral now resting
    own, cursor = {}, None
    for _ in range(40):
        q = {"status": "resting", "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        r = client.get("/portfolio/orders", params=q) or {}
        for o in r.get("orders") or []:
            if not str(o.get("client_order_id", "")).startswith(imm.CLIENT_ORDER_PREFIX + "-"):
                continue
            bp = imm.order_yes_book_cents(o)
            if bp:
                own.setdefault(o["ticker"], []).append((bp[0], bp[1], imm.order_remaining(o)))
        cursor = r.get("cursor")
        if not cursor or not r.get("orders"):
            break
    rest_pvd = sum((px if sd == "bid" else 100 - px) / 100 * q
                   for t, os_ in own.items() if t in L for sd, px, q in os_)
    live_scan = sum(float(e.get("coll_usd") or 0) for t, e in L.items()
                    if str(e.get("added", "")).startswith("scan 20") and ts(e["stop"]) > now and t not in past)
    summary["pvd_resting_collateral"] = round(rest_pvd, 2)
    summary["scan_collateral_live"] = round(live_scan, 2)

    def book(t):
        ob = pget(f"/markets/{t}/orderbook")
        time.sleep(0.25)
        return t, ob
    with ThreadPoolExecutor(4) as ex:
        books = dict(ex.map(book, sorted(past)))

    scored, why = [], {"no book": 0, "no quotable side": 0, "under bar": 0}
    for t, m in past.items():
        ob = books.get(t)
        if not ob:
            why["no book"] += 1
            continue
        pr = cands[t]
        y0, n0 = imm.orderbook_levels(ob)
        y0, n0 = imm.external_levels(y0, n0, own.get(t, []))
        y = {int(p): float(q) for p, q in y0 if q > 0}
        n = {int(p): float(q) for p, q in n0 if q > 0}
        if not y or not n:
            why["no book"] += 1
            continue
        bb, ba = max(y), 100 - max(n)
        kind = "dead" if ba - bb >= 30 else "live"
        qb = ref_px(y) or min(y)
        qa = 100 - (ref_px(n) or min(n))
        last = float(m.get("last_price_dollars") or 0) * 100 or None
        dec = imm.pvd_decided_side(last)
        c = CUSH[kind]
        no_losing = bool((L.get(t) or {}).get("no_losing_side"))
        ok_b = (2 <= qb <= 99) if dec == "bid" else \
            (3 <= qb <= 97 and (last is None or qb <= math.floor(last - c)) and not (no_losing and dec == "ask"))
        ok_a = (2 <= qa <= 99) if dec == "ask" else \
            (3 <= qa <= 97 and (last is None or qa >= math.ceil(last + c)) and not (no_losing and dec == "bid"))
        if not (ok_b or ok_a):
            why["no quotable side"] += 1
            continue
        t_from = max(now, pr["start"])
        hrs = SIM_HOURS or (pr["end"] - t_from).total_seconds() / 3600

        def rate(bs, as_):
            oo = ([("bid", qb, bs)] if ok_b else []) + ([("ask", qa, as_)] if ok_a else [])
            f, _ = imm.estimate_reward_share([[p, q] for p, q in y.items()], [[p, q] for p, q in n.items()],
                                             oo, pr["target"], pr["df"], own_in_book=False)
            return f * pr["pd"]
        # FULL SIZE from the start (Jack 2026-11-05: "want to bank rewards early and
        # overnight when there are less fills. so go at 120 on dead and 80 on live,
        # and up to 300 on a decided side") -- not the smallest size that clears
        s = DEAD_SIZE if kind == "dead" else LIVE_SIZE
        bs = DECIDED_SIZE if dec == "bid" else s
        as_ = DECIDED_SIZE if dec == "ask" else s
        r = rate(bs, as_)
        if r * hrs / 24 < BAR:
            why["under bar"] += 1         # can't clear $1.30 by its program end even at full size
            continue
        coll = (bs * qb / 100 if ok_b else 0) + (as_ * (100 - qa) / 100 if ok_a else 0)
        scored.append(dict(t=t, ev=m.get("event_ticker"), kind=kind, dec=dec, last=last, size=s, bs=bs, as_=as_,
                           rate=r, proj=r * hrs / 24, coll=coll, roi=r / coll if coll else 0.0, pd=pr["pd"],
                           start=pr["start"], end=pr["end"], ext=bool(pr.get("extend")),
                           relit=t in L and not pr.get("extend"), book=f"{bb}x{ba}", qb=qb, qa=qa, c=c))
    scored.sort(key=lambda x: -x["roi"])
    if REPORT:
        import csv
        with open(REPORT, "w", encoding="utf-8") as fh:
            json.dump({"at": zs(now), "skipped": why, "scored": scored}, fh, indent=1, default=str)
        cols = ["t", "ev", "kind", "dec", "last", "book", "bs", "as_", "rate", "proj", "coll", "roi", "pd", "end"]
        with open(os.path.splitext(REPORT)[0] + ".csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for x in scored:
                w.writerow([x.get(c) for c in cols])
        log(f"report: {len(scored)} scored -> {REPORT} (+ .csv)")
    if SIM_HOURS:
        with open(os.path.join(OUT_DIR, "sim_scored.json"), "w", encoding="utf-8") as fh:
            json.dump(scored, fh, default=str)
    used, taken = live_scan, []
    for x in scored:
        if x["ext"]:
            taken.append(x)                 # exposure already resting: not charged to the budget
            continue
        if used + x["coll"] > BUDGET:
            continue
        used += x["coll"]
        taken.append(x)
    summary.update(scored=len(scored), skipped=why, taken_n=len(taken),
                   taken_usd_day=round(sum(x["rate"] for x in taken), 2),
                   taken_collateral=round(sum(x["coll"] for x in taken), 2),
                   over_budget=len(scored) - len(taken))
    log(f"PVD collateral resting now ${rest_pvd:,.0f}; scan adds still live ${live_scan:,.0f}")
    log(f"scored {len(scored)} clearing ${BAR:.2f} | skipped {why} | taking {len(taken)} by ROI "
        f"(extended {sum(1 for x in taken if x['ext'])}, relit {sum(1 for x in taken if x['relit'])}, "
        f"new {sum(1 for x in taken if not x['relit'] and not x['ext'])}), "
        f"new at risk if filled ${sum(x['coll'] for x in taken if not x['ext']):,.0f} (budget ${BUDGET:,.0f}), "
        f"~${sum(x['rate'] for x in taken):,.0f}/day; {len(scored) - len(taken)} over budget")
    for x in taken[:20]:
        log(f"  {'EXTEND' if x['ext'] else 'RELIT ' if x['relit'] else 'NEW   '} {x['t']} {x['kind']} last {x['last']} book {x['book']} "
            f"size {x['bs']}/{x['as_']} ${x['rate']:.2f}/day proj ${x['proj']:.2f} "
            f"ROI {x['roi'] * 100:.1f}%/day {zs(x['start'])[5:16]}->{zs(x['end'])[5:16]}")
    return taken


if __name__ == "__main__":
    sys.exit(main())
