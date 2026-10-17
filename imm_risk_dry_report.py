#!/usr/bin/env python3
"""The 3-day report on the IMM's cross-event breaker and its dollar limits +
skew, both log-only (Jack 2026-10-17: "#2 and #4/#5: #2 log-only, plus #4 and
#5 together as one log-only build. Email report in 3 days").

  ws_group_<UTC date>.jsonl   the cross-event breaker (#2, IMM_SWEEP_GROUP=dry):
                              `trip` lines (with the held side's reward a day)
                              and `held_fill` lines -- our fills on a side a
                              live trip would have been holding
  risk_dry_<UTC date>.jsonl   the dollar limits (#4/#5, IMM_RISK_LIMITS=dry):
                              a `start` line per bot run, a `state` line each
                              time a (market, side) changes between ok / skew /
                              block / none, a `pass` summary every 5 minutes
  fills_<date>.jsonl          our maker fills (fill_id = the WS trade_id)
  dashboard/imm_dashboard.html  the `const D` object: each fill's 5-min and
                              4-hour mark-outs (cents a contract, + = ours)

A fill a rule would have stopped is scored at its 4-hour mark-out: a loss there
is a loss the rule saves. The reward a rule gives up is the reward a day the
bot logged for the side, over the time the rule would hold it: all of it for a
hold or a block, half for a skew (a tick back keeps about half). A skewed
side's fills would have rested a tick back, which costs about half as much, so
they count at half.

Writes run-logs/incentive-mm/risk_dry_report_<ET date>.md and prints its
path. Read-only: no Kalshi call, nothing in the bot touched.

  python imm_risk_dry_report.py [--since ISO] [--until ISO] [--dir DIR]
"""
import argparse
import collections
import glob
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from imm_fast_cancel_report import ET, load_markouts, markout

DEFAULT_DIR = r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm"
GRACE_S = 2.0          # a fill up to 2s after a state ended still counts in it
ALIVE_S = 600.0        # a run's states end at most 10 min after its last line
MIN_TRIPS = 10         # fewer trips (or states) than this: too few to call


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_lines(d, prefix, since, until):
    rows = []
    day = datetime.fromtimestamp(since, timezone.utc).date() - timedelta(days=1)
    last = datetime.fromtimestamp(until, timezone.utc).date() + timedelta(days=1)
    while day <= last:
        p = os.path.join(d, f"{prefix}_{day.isoformat()}.jsonl")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if since - 5 <= float(r.get("ts") or 0) <= until + 5:
                        rows.append(r)
        day += timedelta(days=1)
    rows.sort(key=lambda r: r["ts"])
    return rows


def load_fills(d, since, until):
    """Our maker fills in the window, oldest first."""
    out = []
    lo = datetime.fromtimestamp(since, timezone.utc).date() - timedelta(days=2)
    hi = datetime.fromtimestamp(until, timezone.utc).date() + timedelta(days=1)
    for p in sorted(glob.glob(os.path.join(d, "fills_*.jsonl"))):
        try:
            day = datetime.strptime(os.path.basename(p)[6:16], "%Y-%m-%d").date()
        except ValueError:
            continue
        if not lo <= day <= hi:
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("is_taker") or not r.get("ticker"):
                    continue
                if since - 5 <= float(r.get("ts") or 0) <= until + 5:
                    out.append(r)
    out.sort(key=lambda r: float(r["ts"]))
    return out


class Tally:
    """Fills a rule would have stopped: count, contracts, 4h / 5-min $."""

    def __init__(self):
        self.fills = self.marked = 0
        self.cts = self.m4 = self.m5 = 0.0

    def add(self, fill, marks):
        self.fills += 1
        self.cts += float(fill.get("count") or 0)
        m5, m4 = markout(fill, marks)
        if m4 is not None:
            self.marked += 1
            self.m4 += m4
            self.m5 += m5 or 0.0


def money(x):
    return f"-${-x:,.2f}" if x < 0 else f"${x:,.2f}"


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def verdict(net, n, what, yes="TURN ON"):
    if n < MIN_TRIPS:
        return f"WATCH (only {plural(n, what)}: too few to call)"
    return yes if net > 0 else "KEEP OFF"


# --- #2: the cross-event breaker ------------------------------------------

def group_section(w, rows, fills, marks, days):
    trips = [r for r in rows if r.get("ev") == "trip"]
    held = [r for r in rows if r.get("ev") == "held_fill"]
    w("## #2 Cross-event breaker (log-only)")
    w("")
    if not trips:
        w("No trips in the window: no two events of one group were hit on the "
          "same side within 10 minutes (or the deploy did not land: no "
          "`ws_group` lines).")
        w("")
        return None
    by_id = {str(f.get("fill_id")): f for f in fills if f.get("fill_id")}
    G = collections.defaultdict(lambda: {"trips": 0, "cost": 0.0, "t": Tally(),
                                         "nomatch": 0})
    for r in trips:
        g = G[r["group"]]
        g["trips"] += 1
        g["cost"] += float(r.get("side_day") or 0.0) * float(r.get("hold_s") or 0.0) / 86400.0
    seen = set()
    for r in held:
        tid = str(r.get("trade_id"))
        if tid in seen:
            continue                    # the feed sent the same fill twice
        seen.add(tid)
        g = G[r["group"]]
        f = by_id.get(tid)
        if f is None:
            g["nomatch"] += 1
            continue
        g["t"].add(f, marks)
    tot_cost = sum(g["cost"] for g in G.values())
    tot_saved = -sum(g["t"].m4 for g in G.values())
    net = tot_saved - tot_cost
    n_held = sum(g["t"].fills for g in G.values())
    w(f"**{verdict(net, len(trips), 'trip')}.** Net {money(net)} over the "
      f"window ({money(net / days)}/day): it would have kept out {plural(n_held, 'fill')} "
      f"that lost {money(tot_saved)} at 4 hours, for {money(tot_cost)} of "
      f"reward on the held sides. {plural(len(trips), 'trip')} ({len(trips) / days:.1f}/day).")
    w("")
    w("| group | trips | held fills | contracts | their 4h $ | their 5-min $ | reward given up | net |")
    w("|---|---|---|---|---|---|---|---|")
    for name, g in sorted(G.items(), key=lambda kv: (kv[1]["t"].m4 + kv[1]["cost"])):
        t = g["t"]
        w(f"| {name} | {g['trips']} | {t.fills} | {t.cts:,.0f} | {money(t.m4)} | "
          f"{money(t.m5)} | {money(g['cost'])} | {money(-t.m4 - g['cost'])} |")
    w("")
    marked = sum(g["t"].marked for g in G.values())
    nomatch = sum(g["nomatch"] for g in G.values())
    w(f"Held fills are our real fills (the breaker only logged): with it on, "
      f"the held side's orders would have been pulled at the trip. {marked} of "
      f"{n_held} had 4-hour mark-outs"
      + (f"; {nomatch} held-fill lines had no matching fill row" if nomatch else "")
      + ". The escalating event holds (#1) were live throughout, so this is on top of them.")
    w("")
    return net / days


# --- #4/#5: the dollar limits + skew ----------------------------------------

def risk_intervals(rows, until):
    """Each (market, side)'s block / skew spells: from its `state` line to its
    next one, a new run's `start` line, or ALIVE_S after the run's last
    line (a run that died), whichever is first."""
    open_ = {}
    out = []
    last_seen = None

    def close(k, end):
        o = open_.pop(k)
        o["end"] = max(o["start"], end)
        out.append(o)

    for r in rows:
        ev = r.get("ev")
        if ev == "start":
            end = r["ts"] if last_seen is None else min(r["ts"], last_seen + ALIVE_S)
            for k in list(open_):
                close(k, end)
        elif ev == "state":
            k = (r.get("ticker"), r.get("side"))
            if k in open_:
                close(k, r["ts"])
            if r.get("state") in ("block", "skew"):
                open_[k] = {"key": k, "state": r["state"], "start": r["ts"], "rec": r}
        last_seen = r["ts"]
    end = until if last_seen is None else min(until, last_seen + ALIVE_S)
    for k in list(open_):
        close(k, end)
    return out


def risk_section(w, rows, fills, marks, days, until):
    states = [r for r in rows if r.get("ev") == "state"]
    passes = [r for r in rows if r.get("ev") == "pass"]
    w("## #4/#5 Dollar limits + skew (log-only)")
    w("")
    if not passes and not states:
        w("No `risk_dry` lines in the window: the check never ran (IMM_RISK_LIMITS "
          "off, no WS feed, or the deploy did not land).")
        w("")
        return None, None
    spells = risk_intervals(rows, until)
    by_key = collections.defaultdict(list)
    for s in spells:
        by_key[s["key"]].append(s)
    # the fills each spell would have stopped (block) or moved a tick back (skew)
    for s in spells:
        s["t"] = Tally()
    for f in fills:
        k = (f.get("ticker"), f.get("our_book_side"))
        ts = float(f["ts"])
        for s in by_key.get(k, ()):
            if s["start"] <= ts <= s["end"] + GRACE_S:
                s["t"].add(f, marks)
                break
    res = {}
    for st, share in (("block", 1.0), ("skew", 0.5)):
        sp = [s for s in spells if s["state"] == st]
        cost = sum(float(s["rec"].get("side_day") or 0.0) * share
                   * (s["end"] - s["start"]) / 86400.0 for s in sp)
        loss = sum(s["t"].m4 for s in sp)
        saved = -loss * share
        res[st] = {"spells": sp, "cost": cost, "saved": saved, "net": saved - cost,
                   "fills": sum(s["t"].fills for s in sp),
                   "cts": sum(s["t"].cts for s in sp),
                   "marked": sum(s["t"].marked for s in sp),
                   "m4": loss, "m5": sum(s["t"].m5 for s in sp),
                   "hours": sum(s["end"] - s["start"] for s in sp) / 3600.0}
    b, k = res["block"], res["skew"]
    w(f"**#4 block: {verdict(b['net'], len(b['spells']), 'block spell', 'WORTH BUILDING LIVE')}.** "
      f"Net {money(b['net'])} ({money(b['net'] / days)}/day): the blocked "
      f"sides took {plural(b['fills'], 'fill')} that lost {money(-b['m4'])} at 4 hours, "
      f"for {money(b['cost'])} of reward on those sides.")
    w("")
    w(f"**#5 skew: {verdict(k['net'], len(k['spells']), 'skew spell', 'WORTH BUILDING LIVE')}.** "
      f"Net {money(k['net'])} ({money(k['net'] / days)}/day): the skewed "
      f"sides took {plural(k['fills'], 'fill')} that lost {money(-k['m4'])} at 4 hours "
      f"(half saved a tick back: {money(k['saved'])}), for "
      f"{money(k['cost'])} of reward (half the sides' reward).")
    w("")
    w("| | block (#4) | skew (#5) |")
    w("|---|---|---|")
    w(f"| spells (a market side entering the state) | {len(b['spells'])} | {len(k['spells'])} |")
    w(f"| side-hours in the state | {b['hours']:,.0f} | {k['hours']:,.0f} |")
    w(f"| fills in the state | {b['fills']} ({b['cts']:,.0f} ct) | {k['fills']} ({k['cts']:,.0f} ct) |")
    w(f"| their 4h mark-out | {money(b['m4'])} | {money(k['m4'])} |")
    w(f"| their 5-min mark-out | {money(b['m5'])} | {money(k['m5'])} |")
    w(f"| reward given up | {money(b['cost'])} | {money(k['cost'])} |")
    w(f"| net | {money(b['net'])} | {money(k['net'])} |")
    w("")
    # a cap in place of the block: the side keeps the share that still fits
    cap_cost = sum((1.0 - float(s["rec"].get("fit") or 0.0))
                   * float(s["rec"].get("side_day") or 0.0)
                   * (s["end"] - s["start"]) / 86400.0 for s in b["spells"])
    cap_saved = -sum((1.0 - float(s["rec"].get("fit") or 0.0)) * s["t"].m4
                     for s in b["spells"])
    w(f"A CAP in place of the block (the side shrinks to what still fits under "
      f"the limit, rather than stopping): about {money(cap_saved - cap_cost)} "
      f"net ({money((cap_saved - cap_cost) / days)}/day), saving "
      f"{money(cap_saved)} for {money(cap_cost)} of reward (both scaled by "
      f"the share that would not fit).")
    w("")
    # where it binds
    rows_by = collections.defaultdict(lambda: {"block": 0.0, "skew": 0.0, "loss": 0.0,
                                               "fills": 0})
    for s in spells:
        r = s["rec"]
        who = (f"group: {r.get('group')}" if r.get("by") == "group"
               else f"event: {r.get('event')}")
        share = 1.0 if s["state"] == "block" else 0.5
        rows_by[who][s["state"]] += float(r.get("side_day") or 0.0) * share \
            * (s["end"] - s["start"]) / 86400.0
        rows_by[who]["loss"] += s["t"].m4 * share
        rows_by[who]["fills"] += s["t"].fills
    if rows_by:
        w("Where it binds (the limit each spell hit), top 15 by reward given up:")
        w("")
        w("| limit | reward given up (block) | (skew) | fills | their 4h $ (skew at half) |")
        w("|---|---|---|---|---|")
        top = sorted(rows_by.items(), key=lambda kv: -(kv[1]["block"] + kv[1]["skew"]))
        for who, v in top[:15]:
            w(f"| {who} | {money(v['block'])} | {money(v['skew'])} | {v['fills']} | "
              f"{money(v['loss'])} |")
        w("")
    if passes:
        p = passes[-1]
        at = datetime.fromtimestamp(p["ts"], ET).strftime("%a %m/%d %H:%M ET")
        w(f"The book at the last summary ({at}): worst case "
          f"{money(-float(p.get('worst_total') or 0.0))} over {p.get('events')} events "
          f"(exempt {money(-float(p.get('exempt_worst') or 0.0))}); {p.get('block')} "
          f"sides would block (~{money(float(p.get('block_day') or 0.0))}/day), "
          f"{p.get('skew')} would skew (~{money(float(p.get('skew_day') or 0.0))}/day).")
        w("")
        groups = p.get("groups") or {}
        if groups:
            w("| group | worst case | limit | used | reward/day | long $ at risk | short $ at risk |")
            w("|---|---|---|---|---|---|---|")
            for g, v in sorted(groups.items(), key=lambda kv: kv[1].get("worst", 0.0)):
                lim = float(v.get("limit") or 0.0)
                used = -float(v.get("worst") or 0.0) / lim if lim else 0.0
                w(f"| {g} | {money(-float(v.get('worst') or 0.0))} | {money(lim)} | "
                  f"{used:.0%} | {money(float(v.get('day') or 0.0))} | "
                  f"{money(float(v.get('long') or 0.0))} | {money(float(v.get('short') or 0.0))} |")
            w("")
        top = p.get("top_events") or []
        if top:
            w("Events past half their limit: " + "; ".join(
                f"{e} {money(-w0)} of {money(lim)} ({u:.0%})" for u, e, w0, lim in top[:10]) + ".")
            w("")
    marked = b["marked"] + k["marked"]
    total = b["fills"] + k["fills"]
    w(f"Data: {len(states)} state lines, {len(passes)} summaries; {marked} of "
      f"{total} fills in a block or skew had 4-hour mark-outs.")
    w("")
    return b["net"] / days, k["net"] / days


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--since", help="UTC ISO; default the first line of either log")
    ap.add_argument("--until", help="UTC ISO; default now")
    args = ap.parse_args(argv)
    d = args.dir
    until = _ts(args.until) if args.until else datetime.now(timezone.utc).timestamp()
    since = _ts(args.since) if args.since else until - 4 * 86400
    grp = load_lines(d, "ws_group", since, until)
    risk = load_lines(d, "risk_dry", since, until)
    if not args.since:
        firsts = [r[0]["ts"] for r in (grp, risk) if r]
        if firsts:
            since = min(firsts)
    days = max((until - since) / 86400.0, 1e-9)
    fills = load_fills(d, since, until)
    marks = load_markouts(d)
    L = []
    w = L.append
    t0 = datetime.fromtimestamp(since, ET).strftime("%a %m/%d %H:%M")
    t1 = datetime.fromtimestamp(until, ET).strftime("%a %m/%d %H:%M ET")
    w(f"# IMM risk rules, log-only: {days:.1f}-day report ({t0} to {t1})")
    w("")
    w("Two rules have run log-only since 10/17: the cross-event breaker (#2) "
      "and the dollar limits with inventory skew (#4/#5). Each would turn on "
      "by its own switch (IMM_SWEEP_GROUP=on; the limits have no live mode "
      "yet). Your call.")
    w("")
    n2 = group_section(w, grp, fills, marks, days)
    n4, n5 = risk_section(w, risk, fills, marks, days, until)
    parts = []
    for name, v in (("#2", n2), ("#4", n4), ("#5", n5)):
        if v is not None:
            parts.append(f"{name} {money(v)}/day")
    if parts:
        w("Net per day: " + ", ".join(parts) + " (positive = the rule would have "
          "made money: losses it would have kept out, less the reward it would "
          "have given up).")
        w("")
    w(f"Scored by imm_risk_dry_report.py from {len(grp)} ws_group lines, "
      f"{len(risk)} risk_dry lines and {len(fills)} maker fills.")
    return finish(d, L)


def finish(d, lines):
    name = f"risk_dry_report_{datetime.now(ET).strftime('%Y-%m-%d')}.md"
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
