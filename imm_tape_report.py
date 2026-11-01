#!/usr/bin/env python3
"""The tape trigger's dry-run score (TAPE_TRIGGER, Jack 2026-11-01: "build
this", on "React to other traders' prints").

Since 2026-11-01 the IMM reads Kalshi's PUBLIC trade channel for the markets
it watches. A taker burst on one market (TAPE_MIN_CT+ contracts over
TAPE_MIN_LEVELS+ prices, or TAPE_BIG_CT+ at one) trips its event: dry, the
trip is logged and the window the hold would keep is watched for our fills.
This nets what a live pull would have saved against what it would have cost:

  avoided   our maker fills in the event inside a trip's window, at least
            --react-s after the trip (sooner = the same sweep, which no pull
            beats), marked out: a fill that lost is a loss avoided
  forgone   the event's reward a day (logged with the trip) for the hold
            plus --requote-s, the wait for the next cycle to re-quote

The trigger's own trips (as run) come first, then other lines re-scored off
the `burst` lines (every burst of TAPE_LOG_MIN_CT+ in an event or group we
rested in) and our fills. Inputs, in the bot's STATUS_DIR:

  ws_tape_<UTC date>.jsonl       trip / held_fill / burst lines
  fills_<date>.jsonl             our fills (the re-scored lines' held fills)
  dashboard/imm_dashboard.html   the `const D` object: each fill's 5-min and
                                 4-hour mark-outs (cents a contract, + = ours)
  reward_est_hourly_cache.json   our reward per market-hour (the re-scored
                                 lines' cost)

Writes run-logs/incentive-mm/tape_report_<ET date>.md and prints its path.
Read-only: no Kalshi call, nothing in the bot touched.

  python imm_tape_report.py [--since YYYY-MM-DD] [--until YYYY-MM-DD]
                            [--dir DIR] [--react-s 1.0] [--requote-s 30]
"""
import argparse
import collections
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from imm_fast_cancel_report import ET, load_markouts, load_reward_rates, markout

DEFAULT_DIR = r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm"
LIVE_MODES = ("dry", "on")          # the trips a live trigger would act on
# the re-scored lines: (label, min contracts, min prices, big one-price size)
RULES = (
    ("as run: 25+ over 2+ prices, or 100+", 25.0, 2, 100.0),
    ("2+ prices, any size", 0.0, 2, 1e18),
    ("25+ contracts", 25.0, 1, 0.0),
    ("50+ contracts", 50.0, 1, 0.0),
    ("100+ contracts", 100.0, 1, 0.0),
    ("50+ over 2+ prices", 50.0, 2, 1e18),
)


def family(series):
    try:
        import imm_dashboard
        return imm_dashboard.family_of(series)[0]
    except Exception:
        return series


def series_of(ticker):
    return (ticker or "").split("-", 1)[0]


def event_of(ticker):
    """The bot's _event_of: the ticker less its last part."""
    return (ticker or "").rsplit("-", 1)[0]


def hold_of(series):
    """The trigger's hold when a line does not carry one (sweep_hold_secs:
    600 s, KXRAIN* 120 s; the approval twins' linked 600 s)."""
    return 120.0 if series.startswith("KXRAIN") else 600.0


def _days(since, until):
    d = since
    while d <= until:
        yield d
        d += timedelta(days=1)


def load_tape(d, since, until):
    """trip / held_fill / burst lines of the UTC days since..until."""
    out = {"trip": [], "held_fill": [], "burst": []}
    for day in _days(since, until):
        p = os.path.join(d, f"ws_tape_{day.isoformat()}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("ev") in out:
                    out[r["ev"]].append(r)
    for rows in out.values():
        rows.sort(key=lambda r: float(r.get("ts") or 0))
    return out


def load_our_fills(d, since, until):
    """Our maker fills by the bot's event key (ticker less its last part)."""
    by_event = collections.defaultdict(list)
    for day in _days(since - timedelta(days=1), until + timedelta(days=1)):
        p = os.path.join(d, f"fills_{day.isoformat()}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("is_taker") or not r.get("ticker"):
                    continue
                by_event[event_of(r["ticker"])].append(r)
    for rows in by_event.values():
        rows.sort(key=lambda r: float(r.get("ts") or 0))
    return by_event


def avoided_of(fills, marks):
    """($ avoided at 5 min, at 4 h, fills unmarked): minus each fill's
    mark-out (+ = the fill lost money, so avoiding it paid)."""
    a5 = a4 = 0.0
    unmarked = 0
    for fl in fills:
        m5, m4 = markout(fl, marks)
        if m5 is None and m4 is None:
            unmarked += 1
        a5 -= m5 or 0.0
        a4 -= m4 or 0.0
    return a5, a4, unmarked


def score_as_run(tape, marks, react_s, requote_s):
    """The trigger's own trips: per mode counts, and for the trips a live
    trigger would act on, $ avoided vs $ forgone (overall, by day, family)."""
    trips = tape["trip"]
    modes = collections.Counter(t.get("mode") for t in trips)
    acting = [t for t in trips if t.get("mode") in LIVE_MODES]
    held = [h for h in tape["held_fill"] if h.get("scope") == "event"
            and h.get("mode") in LIVE_MODES]
    by_trip = collections.defaultdict(list)
    early = 0
    for h in held:
        if float(h.get("since_trip_s") or 0.0) < react_s:
            early += 1                       # the same sweep: no pull beats it
            continue
        by_trip[(h.get("trip_event"), round(float(h.get("trip_ts") or 0), 3))].append(
            {"ts": h.get("fill_ts"), "ticker": h.get("ticker"), "count": h.get("count")})
    rows = []
    for t in acting:
        fills = by_trip.get((t.get("event"), round(float(t.get("ts") or 0), 3)), [])
        a5, a4, unm = avoided_of(fills, marks)
        cost = float(t.get("event_day") or 0.0) * \
            (float(t.get("hold_s") or hold_of(series_of(t.get("ticker")))) + requote_s) / 86400.0
        day = datetime.fromtimestamp(float(t["ts"]), timezone.utc).date()
        rows.append({"trip": t, "day": day, "fam": family(series_of(t.get("ticker"))),
                     "fills": len(fills), "unmarked": unm, "a5": a5, "a4": a4,
                     "cost": cost})
    exempt = [t for t in trips if t.get("mode") == "exempt"]
    ex_fills = [h for h in tape["held_fill"] if h.get("scope") == "event"
                and h.get("mode") == "exempt"
                and float(h.get("since_trip_s") or 0.0) >= react_s]
    ex = avoided_of([{"ts": h.get("fill_ts"), "ticker": h.get("ticker"),
                      "count": h.get("count")} for h in ex_fills], marks)
    grp = [h for h in tape["held_fill"] if h.get("scope") == "group"
           and float(h.get("since_trip_s") or 0.0) >= react_s]
    g = avoided_of([{"ts": h.get("fill_ts"), "ticker": h.get("ticker"),
                     "count": h.get("count")} for h in grp], marks)
    return {"modes": modes, "rows": rows, "early": early,
            "exempt": (len(exempt), len(ex_fills)) + ex,
            "group": (len(grp),) + g}


def rescore(tape, fills_by_event, marks, rates, rule, react_s, requote_s):
    """One line re-scored off the burst lines: a burst meeting it trips at its
    last print (t1); a trip inside its event's open window starts nothing
    (as live); the window's fills of ours after t1 + react_s are avoided;
    the cost is our reward an hour over the event's markets for the hold."""
    _label, min_ct, min_lv, big = rule
    by_event = collections.defaultdict(set)
    for (tk, _h) in rates:
        by_event[event_of(tk)].add(tk)
    open_until = {}
    n = a5 = a4 = cost = 0.0
    fills_n = unmarked = 0
    for b in tape["burst"]:
        ct, lv = float(b.get("count") or 0.0), int(b.get("levels") or 0)
        if not (ct + 1e-9 >= min_ct and (lv >= min_lv or ct + 1e-9 >= big)):
            continue
        ev = b.get("event") or event_of(b.get("ticker"))
        t1 = float(b.get("t1") or b.get("ts") or 0.0)
        if open_until.get(ev, 0.0) > t1:
            continue
        hold = hold_of(series_of(b.get("ticker")))
        open_until[ev] = t1 + hold
        n += 1
        got = [f for f in fills_by_event.get(ev, ())
               if t1 + react_s <= float(f.get("ts") or 0) < t1 + hold]
        x5, x4, unm = avoided_of(got, marks)
        a5 += x5
        a4 += x4
        fills_n += len(got)
        unmarked += unm
        hour = int(t1 // 3600)
        per_h = sum(rates.get((tk, hour), 0.0) for tk in by_event.get(ev, ()))
        cost += per_h * (hold + requote_s) / 3600.0
    return {"trips": int(n), "fills": fills_n, "unmarked": unmarked,
            "a5": a5, "a4": a4, "cost": cost}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    today = datetime.now(timezone.utc).date()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--since", default="2026-11-01")
    ap.add_argument("--until", default=today.isoformat())
    ap.add_argument("--react-s", type=float, default=1.0)
    ap.add_argument("--requote-s", type=float, default=30.0)
    a = ap.parse_args(argv)
    since, until = date.fromisoformat(a.since), date.fromisoformat(a.until)
    days = (until - since).days + 1
    tape = load_tape(a.dir, since, until)
    marks = load_markouts(a.dir)
    rates = load_reward_rates(a.dir)
    fills = load_our_fills(a.dir, since, until)
    s = score_as_run(tape, marks, a.react_s, a.requote_s)
    rows = s["rows"]
    A5, A4, C = (sum(r[k] for r in rows) for k in ("a5", "a4", "cost"))
    L = [f"# Tape trigger, dry run: {since} to {until} ({days} UTC day(s))", ""]
    L.append(f"**As run:** {len(rows)} trips it would have acted on, "
             f"{sum(r['fills'] for r in rows)} of our fills inside their holds. "
             f"Avoided ${A5:+,.2f} at 5 min / ${A4:+,.2f} at 4 h, against "
             f"${C:,.2f} of reward the holds would have cost: net "
             f"**${A5 - C:+,.2f} / ${A4 - C:+,.2f}** "
             f"(${(A5 - C) / days:+,.2f} / ${(A4 - C) / days:+,.2f} a day).")
    L.append("")
    L.append("Trips by mode: " + ", ".join(f"{m} {n}" for m, n in s["modes"].most_common())
             + f". Fills under {a.react_s:g}s after their trip (the same sweep, "
             f"not counted): {s['early']}. Unmarked fills: "
             f"{sum(r['unmarked'] for r in rows)}.")
    ne, nf, e5, e4, _u = s["exempt"]
    L.append(f"Mention series (exempt, never pulled): {ne} trips, {nf} fills "
             f"inside, ${e5:+,.2f} / ${e4:+,.2f} that a pull would have avoided.")
    ng, g5, g4, _gu = s["group"]
    L.append(f"Group scope (logged only): {ng} of our fills in a tripped "
             f"group's OTHER events, ${g5:+,.2f} / ${g4:+,.2f}.")
    L += ["", "| UTC day | trips | fills | avoided 5m | avoided 4h | forgone | net 5m | net 4h |",
          "|---|---:|---:|---:|---:|---:|---:|---:|"]
    by_day = collections.defaultdict(list)
    for r in rows:
        by_day[r["day"]].append(r)
    for d in sorted(by_day):
        rs = by_day[d]
        x5, x4, c = (sum(r[k] for r in rs) for k in ("a5", "a4", "cost"))
        L.append(f"| {d} | {len(rs)} | {sum(r['fills'] for r in rs)} | ${x5:+,.2f} "
                 f"| ${x4:+,.2f} | ${c:,.2f} | ${x5 - c:+,.2f} | ${x4 - c:+,.2f} |")
    L += ["", "| family | trips | fills | avoided 5m | avoided 4h | forgone | net 4h |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    by_fam = collections.defaultdict(list)
    for r in rows:
        by_fam[r["fam"]].append(r)
    for fam, rs in sorted(by_fam.items(), key=lambda kv: -sum(r["a4"] - r["cost"]
                                                                 for r in kv[1])):
        x5, x4, c = (sum(r[k] for r in rs) for k in ("a5", "a4", "cost"))
        L.append(f"| {fam} | {len(rs)} | {sum(r['fills'] for r in rs)} | ${x5:+,.2f} "
                 f"| ${x4:+,.2f} | ${c:,.2f} | ${x4 - c:+,.2f} |")
    top = sorted(rows, key=lambda r: -abs(r["a4"]))[:10]
    if top:
        L += ["", "Biggest trips (by 4-h $):", ""]
        for r in top:
            t = r["trip"]
            L.append(f"- {datetime.fromtimestamp(float(t['ts']), timezone.utc):%m-%d %H:%M:%SZ} "
                     f"{t.get('ticker')} {t.get('hits')}s taken {t.get('count')} over "
                     f"{t.get('levels')} price(s), seen {t.get('lag_s')}s in: "
                     f"{r['fills']} fill(s), ${r['a5']:+,.2f} / ${r['a4']:+,.2f}, "
                     f"cost ${r['cost']:,.2f}")
        if len(rows) > 2:
            rest = sorted(rows, key=lambda r: -(r["a4"] - r["cost"]))[2:]
            L.append(f"- Without the best two trips: net "
                     f"${sum(r['a4'] - r['cost'] for r in rest):+,.2f} at 4 h.")
    L += ["", f"Other lines, re-scored off {len(tape['burst'])} burst lines (a "
          f"burst trips at its last print; cost = our reward an hour over the "
          f"event's markets):", "",
          "| line | trips | fills | avoided 5m | avoided 4h | forgone | net 4h / day |",
          "|---|---:|---:|---:|---:|---:|---:|"]
    for rule in RULES:
        x = rescore(tape, fills, marks, rates, rule, a.react_s, a.requote_s)
        L.append(f"| {rule[0]} | {x['trips']} | {x['fills']} | ${x['a5']:+,.2f} "
                 f"| ${x['a4']:+,.2f} | ${x['cost']:,.2f} "
                 f"| ${(x['a4'] - x['cost']) / days:+,.2f} |")
    L += ["", "Caveats: a fill a hold avoids may come later instead (the bot "
          "re-quotes at the moved book); the reward is the bot's model (Kalshi "
          "pays ~1.1x); 4-h marks understate settlement swings; a restart drops "
          "the open dry windows, so a few held fills go unlogged."]
    name = f"tape_report_{datetime.now(ET).strftime('%Y-%m-%d')}.md"
    path = os.path.join(a.dir, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
