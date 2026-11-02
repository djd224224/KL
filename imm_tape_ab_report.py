#!/usr/bin/env python3
"""The tape trigger's LIVE A/B score (IMM_TAPE_TRIGGER=ab, Jack 2026-11-02:
"Build it", on "Should I build the live A/B test for you to switch on?").

In the A/B a taker of TAPE_AB_MIN_CT+ contracts in one burst opens a WINDOW on
its event, and a hash of the event and the print draws its arm: PAUSE (every
order of ours there pulled and held TAPE_HOLD_SECS) or CONTROL (logged only).
A later trip inside a window extends it. The arms are compared window by
window, so the cost of re-quoting after a pause needs no model. Per window:

  span     from its first trip to POST_S after its end, cut at the key's next
           window
  fills    the mark-outs of our maker fills in the key inside the span, at
           5 min and 4 h (the dashboard's; + = the fills made money)
  forgone  PAUSE only: the event's reward a day (logged with the trip) x its
           dark time (the first trip to our first order back in the key, from
           the orders log) x REWARD_X
  value    fills - forgone

effect = mean value(pause) - mean value(control), x the windows a day: what
pausing every window would add a day. 90% CI: a bootstrap over UTC days.
The verdict follows tape_replay_long\\PREREGISTRATION_LIVE_AB.md:

  STOP          the paused windows trail by STOP_USD at 4 h (checked any day)
  RUNNING       fewer than MIN_DAYS days of the A/B so far
  GO            the 4 h effect > 0 with its CI's lower end > 0, and 5 min >= 0
  NO-GO         the 4 h effect <= 0
  INCONCLUSIVE  otherwise

Inputs, in the bot's STATUS_DIR: ws_tape_<UTC date>.jsonl (the A/B trip
lines), fills_<date>.jsonl, orders_<date>.jsonl, dashboard/imm_dashboard.html.
Writes tape_ab_report_<ET date>.md (+ .json) and prints its path and the
verdict. Read-only: no Kalshi call, nothing in the bot touched.

  python imm_tape_ab_report.py [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--dir DIR]
"""
import argparse
import collections
import hashlib
import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone

from imm_fast_cancel_report import ET, load_markouts, markout
from imm_tape_report import DEFAULT_DIR, event_of, family, load_tape, series_of

POST_S = 420.0             # a span runs 7 min past its window (re-quote + 6 min)
REWARD_X = 1.25            # the forgone reward at 1.25x the bot's model
REQUOTE_FALLBACK_S = 16.2  # dark past the hold when no order of ours came back
REQUOTE_MAX_S = 1800.0     # ...looked for this long after the hold
STOP_USD = 150.0
MIN_DAYS = 21
BOOT_N = 2000
SEED = 20261102
HZ = ("5m", "4h")


def _days(since, until):
    d = since
    while d <= until:
        yield d
        d += timedelta(days=1)


def _iso(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _day(ts):
    return datetime.fromtimestamp(float(ts), timezone.utc).date()


def key_of_trip(r):
    """The bot's hold key (sweep_hold_key): the event, or "group:" + its
    linked series."""
    return "group:" + ",".join(r["linked"]) if r.get("linked") else r.get("event")


def draw_of(key, salt):
    """tape_ab_arm's draw, recomputed for the audit."""
    h = hashlib.sha256(f"{key}|{salt}".encode("utf-8")).hexdigest()
    return int(h[:13], 16) / float(16 ** 13)


def windows_of(trips):
    """The A/B windows from the trip lines, each {key, arm, mode, at, until,
    span_end, ...}: an opening line (mode on / dry, no "extend") starts one,
    an extension line ("inside" with "ab") moves its end."""
    wins = {}
    for r in trips:
        if not r.get("ab"):
            continue
        k = key_of_trip(r)
        at = float(r.get("window_at") or r.get("ts") or 0.0)
        wid = (k, round(at, 3))
        until = float(r.get("until") or at)
        if r.get("mode") in ("on", "dry") and "extend" not in r:
            salt = r.get("cross_tid") or f"{float(r.get('t1') or 0.0):.3f}"
            wins[wid] = {"key": k, "arm": r["ab"], "mode": r["mode"], "at": at,
                         "until": until, "event": r.get("event"),
                         "ticker": r.get("ticker"),
                         "event_day": float(r.get("event_day") or 0.0),
                         "u": r.get("ab_u"), "salt": salt, "frac": r.get("ab_frac"),
                         "trips": 1, "fam": family(series_of(r.get("ticker")))}
        elif wid in wins:
            w = wins[wid]
            w["until"] = max(w["until"], until)
            w["trips"] += 1
    out = sorted(wins.values(), key=lambda w: (w["key"], w["at"]))
    for i, w in enumerate(out):
        nxt = out[i + 1]["at"] if i + 1 < len(out) and out[i + 1]["key"] == w["key"] else None
        w["span_end"] = w["until"] + POST_S if nxt is None else min(w["until"] + POST_S, nxt)
    return out


def linked_of(trips):
    """series -> its linked hold key, from the trip lines' "linked" lists."""
    return {s: "group:" + ",".join(r["linked"])
            for r in trips if r.get("linked") for s in r["linked"]}


def key_of_ticker(t, linked):
    return linked.get(series_of(t)) or event_of(t)


def load_fills(d, since, until, linked):
    """Our maker fills by hold key, once per fill_id."""
    by_key = collections.defaultdict(list)
    seen = set()
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
                t = r.get("ticker")
                fid = r.get("fill_id")
                if r.get("is_taker") or not t or (fid and fid in seen):
                    continue
                if fid:
                    seen.add(fid)
                by_key[key_of_ticker(t, linked)].append(r)
    for rows in by_key.values():
        rows.sort(key=lambda r: float(r.get("ts") or 0))
    return by_key


def load_places(d, since, until, keys, linked):
    """When we placed orders, by hold key (only the keys asked)."""
    out = collections.defaultdict(list)
    for day in _days(since - timedelta(days=1), until + timedelta(days=1)):
        p = os.path.join(d, f"orders_{day.isoformat()}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                if '"kind": "place"' not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                k = key_of_ticker(r.get("ticker") or "", linked)
                ts = _iso(r.get("ts"))
                if k in keys and ts is not None:
                    out[k].append(ts)
    for v in out.values():
        v.sort()
    return out


def score_windows(wins, fills_by_key, places, marks):
    """Each window's fills, dark time, forgone reward and value (in place)."""
    for w in wins:
        rows = [f for f in fills_by_key.get(w["key"], ())
                if w["at"] <= float(f.get("ts") or 0) < w["span_end"]]
        val = {h: 0.0 for h in HZ}
        unm = 0
        for f in rows:
            m5, m4 = markout(f, marks)
            if m5 is None and m4 is None:
                unm += 1
            val["5m"] += m5 or 0.0
            val["4h"] += m4 or 0.0
        dark = forgone = 0.0
        if w["arm"] == "pause" and w["mode"] == "on":
            back = next((x for x in places.get(w["key"], ())
                         if w["until"] <= x <= w["until"] + REQUOTE_MAX_S), None)
            end = back if back is not None else w["until"] + REQUOTE_FALLBACK_S
            dark = max(0.0, end - w["at"])
            forgone = w["event_day"] * dark / 86400.0 * REWARD_X
        w.update(fills=len(rows), unmarked=unm, fill_val=val, dark_s=dark,
                 forgone=forgone, value={h: val[h] - forgone for h in HZ})
    return wins


def arms_of(wins):
    """(the windows that paused, the controls): a pause-arm window that ran
    dry (the sweep breaker off) never paused, so it is in neither."""
    return ([w for w in wins if w["arm"] == "pause" and w["mode"] == "on"],
            [w for w in wins if w["arm"] == "control"])


def _mean(ws, h):
    return sum(w["value"][h] for w in ws) / len(ws)


def effect_of(wins, n_days):
    """$/day if every window paused: (mean pause - mean control) x windows a
    day; None without both arms."""
    p, c = arms_of(wins)
    if not p or not c or n_days <= 0:
        return None
    per_day = (len(p) + len(c)) / float(n_days)
    return {h: (_mean(p, h) - _mean(c, h)) * per_day for h in HZ}


def boot_ci(wins, days, n=BOOT_N, seed=SEED):
    """90% CI of effect_of per horizon: UTC days resampled with replacement
    (a day without windows counts too)."""
    by_day = collections.defaultdict(list)
    for w in wins:
        by_day[_day(w["at"])].append(w)
    ds = sorted(days)
    rng = random.Random(seed)
    got = {h: [] for h in HZ}
    for _ in range(n):
        sample = [w for _k in ds for w in by_day.get(ds[rng.randrange(len(ds))], ())]
        e = effect_of(sample, len(ds))
        if e is not None:
            for h in HZ:
                got[h].append(e[h])
    out = {}
    for h in HZ:
        b = sorted(got[h])
        out[h] = (b[int(0.05 * len(b))], b[int(0.95 * len(b)) - 1]) if b else (None, None)
    return out


def shortfall_of(wins):
    """$ the paused windows trail (or lead) the controls at 4 h: (mean pause -
    mean control) x the paused windows."""
    p, c = arms_of(wins)
    if not p or not c:
        return None
    return (_mean(p, "4h") - _mean(c, "4h")) * len(p)


def daily_of(wins, days):
    """One row per UTC day: its paused and control windows; what the day's
    pauses made against the day's controls, (mean pause - mean control) x
    paused windows; what pausing every window that day would have made; and
    the running total through the day, all days pooled (the stop rule's
    number) -- each at 5 min and 4 h, None without both arms."""
    p_all, c_all = arms_of(wins)
    rows = []
    for d in sorted(days):
        p = [w for w in p_all if _day(w["at"]) == d]
        c = [w for w in c_all if _day(w["at"]) == d]
        pu = [w for w in p_all if _day(w["at"]) <= d]
        cu = [w for w in c_all if _day(w["at"]) <= d]
        row = {"day": d.isoformat(), "pause": len(p), "control": len(c),
               "forgone": sum(w["forgone"] for w in p)}
        for h in HZ:
            diff = _mean(p, h) - _mean(c, h) if p and c else None
            row[f"impact_{h}"] = None if diff is None else diff * len(p)
            row[f"all_on_{h}"] = None if diff is None else diff * (len(p) + len(c))
            row[f"so_far_{h}"] = ((_mean(pu, h) - _mean(cu, h)) * len(pu)
                                  if pu and cu else None)
        rows.append(row)
    return rows


def verdict_of(eff, ci, short, n_days):
    if short is not None and short < -STOP_USD:
        return "STOP"
    if n_days < MIN_DAYS:
        return "RUNNING"
    if eff is None or ci["4h"][0] is None:
        return "INCONCLUSIVE"
    if eff["4h"] > 0 and ci["4h"][0] > 0 and eff["5m"] >= 0:
        return "GO"
    if eff["4h"] <= 0:
        return "NO-GO"
    return "INCONCLUSIVE"


WHAT = {"STOP": "the paused half is behind by more than the stop line: back to dry",
        "RUNNING": "keep it running",
        "GO": "pausing wins: switching every window to pause is Jack's call",
        "NO-GO": "pausing does not pay: back to dry",
        "INCONCLUSIVE": "no clear answer: run on, or stop (Jack's call)"}


def build(d, since, until):
    """Score the A/B from the logs in `d`, UTC days since..until: every
    window's value, the arms, the effect and its CI, the stop rule's
    shortfall, the verdict, the per-day rows and the checks."""
    tape = load_tape(d, since, until)
    trips = tape["trip"]
    wins = windows_of(trips)
    ctx = {"since": since, "until": until, "wins": wins, "first": None, "days": [],
           "n_days": 0, "p": [], "c": [], "eff": None, "ci": None, "short": None,
           "verdict": "RUNNING", "daily": [], "dry_pause": [], "frac": None, "bad_draws": 0}
    if not wins:
        return ctx
    linked = linked_of(trips)
    first = _day(min(w["at"] for w in wins))
    days = list(_days(first, until))
    fills = load_fills(d, first, until, linked)
    places = load_places(d, first, until, {w["key"] for w in wins}, linked)
    score_windows(wins, fills, places, load_markouts(d))
    p, c = arms_of(wins)
    eff = effect_of(wins, len(days))
    ci = boot_ci(wins, days)
    short = shortfall_of(wins)
    ctx.update(first=first, days=days, n_days=len(days), p=p, c=c, eff=eff, ci=ci,
               short=short, verdict=verdict_of(eff, ci, short, len(days)),
               daily=daily_of(wins, days),
               dry_pause=[w for w in wins if w["arm"] == "pause" and w["mode"] != "on"],
               frac=next((w["frac"] for w in wins if w.get("frac") is not None), None),
               bad_draws=sum(1 for w in wins if w.get("u") is not None
                             and abs(draw_of(w["key"], w["salt"]) - float(w["u"])) > 1e-5))
    return ctx


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    today = datetime.now(timezone.utc).date()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--since", default="2026-11-02")
    ap.add_argument("--until", default=today.isoformat())
    a = ap.parse_args(argv)
    since, until = date.fromisoformat(a.since), date.fromisoformat(a.until)
    ctx = build(a.dir, since, until)
    wins, p, c = ctx["wins"], ctx["p"], ctx["c"]
    verdict, eff, ci, short, n_days = (ctx["verdict"], ctx["eff"], ctx["ci"],
                                       ctx["short"], ctx["n_days"])
    L = []
    if not wins:
        L.append(f"# Tape trigger live A/B: {since} to {until}")
        L.append("")
        L.append("No A/B windows yet (IMM_TAPE_TRIGGER is not ab, or nothing has tripped).")
    else:
        L.append(f"# Tape trigger live A/B: {ctx['first']} to {until} ({n_days} UTC day(s))")
        L.append("")
        L.append(f"**Verdict: {verdict}** -- {WHAT[verdict]}.")
        L.append("")
        if eff is not None:
            L.append(f"Pausing every window would add **${eff['4h']:+,.2f}/day at 4 h** "
                     f"(90% CI ${ci['4h'][0]:+,.2f} to ${ci['4h'][1]:+,.2f}) and "
                     f"${eff['5m']:+,.2f}/day at 5 min (CI ${ci['5m'][0]:+,.2f} to "
                     f"${ci['5m'][1]:+,.2f}). The paused windows are "
                     f"${short:+,.2f} against the controls at 4 h (stop at "
                     f"-${STOP_USD:,.0f}). Day {n_days} of {MIN_DAYS}.")
        else:
            L.append("Not both arms yet: no effect to score.")
        L += ["", "| arm | windows | trips | fills | unmarked | mean value 5m | "
              "mean value 4h | mean dark s | forgone |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for name, ws in (("pause", p), ("control", c)):
            if not ws:
                L.append(f"| {name} | 0 | | | | | | | |")
                continue
            L.append(f"| {name} | {len(ws)} | {sum(w['trips'] for w in ws)} | "
                     f"{sum(w['fills'] for w in ws)} | {sum(w['unmarked'] for w in ws)} | "
                     f"${_mean(ws, '5m'):+,.3f} | ${_mean(ws, '4h'):+,.3f} | "
                     f"{sum(w['dark_s'] for w in ws) / len(ws):,.0f} | "
                     f"${sum(w['forgone'] for w in ws):,.2f} |")
        dry_pause, frac = ctx["dry_pause"], ctx["frac"]
        n_ab = len(p) + len(c) + len(dry_pause)
        L.append("")
        L.append(f"Checks: {len(p) + len(dry_pause)} of {n_ab} windows drew pause"
                 + (f" (expected {frac:.0%})" if frac is not None else "")
                 + f"; {ctx['bad_draws']} draw(s) that do not recompute from the log"
                 + (f"; {len(dry_pause)} pause window(s) ran dry (the sweep breaker "
                    f"was off) and are left out" if dry_pause else "") + ".")

        def _m(x):
            return "" if x is None else f"${x:+,.2f}"
        L += ["", "By UTC day (pauses vs that day's controls, net of the reward they "
              "gave up; 'so far' pools every day, as the stop rule does):", "",
              "| day | paused | controls | pauses 4h | pauses 5m | if all paused 4h | "
              "so far 4h |", "|---|---:|---:|---:|---:|---:|---:|"]
        for r in ctx["daily"]:
            L.append(f"| {r['day']} | {r['pause']} | {r['control']} | {_m(r['impact_4h'])} | "
                     f"{_m(r['impact_5m'])} | {_m(r['all_on_4h'])} | {_m(r['so_far_4h'])} |")
        by_fam = collections.defaultdict(lambda: ([], []))
        for w in p:
            by_fam[w["fam"]][0].append(w)
        for w in c:
            by_fam[w["fam"]][1].append(w)
        L += ["", "By family (context only; small cells are noise):", "",
              "| family | pause | control | mean pause 4h | mean control 4h |",
              "|---|---:|---:|---:|---:|"]
        for fam, (fp, fc) in sorted(by_fam.items(), key=lambda kv: -len(kv[1][0]) - len(kv[1][1])):
            L.append(f"| {fam} | {len(fp)} | {len(fc)} | "
                     + (f"${_mean(fp, '4h'):+,.3f}" if fp else "") + " | "
                     + (f"${_mean(fc, '4h'):+,.3f}" if fc else "") + " |")
        top = sorted(p + c, key=lambda w: -abs(w["value"]["4h"]))[:8]
        if top:
            L += ["", "Biggest windows (by 4-h $):", ""]
            for w in top:
                L.append(f"- {datetime.fromtimestamp(w['at'], timezone.utc):%m-%d %H:%M:%SZ} "
                         f"{w['arm']} {w['key']}: {w['trips']} trip(s), {w['fills']} fill(s), "
                         f"${w['fill_val']['4h']:+,.2f} at 4 h, forgone ${w['forgone']:,.2f}")
    L += ["", f"Method: span = first trip to {POST_S:.0f}s after the window ends (cut at "
          f"the key's next window); forgone = the trip's reward a day x dark time "
          f"x {REWARD_X:g}; marks from the dashboard (5 min, 4 h). Rule: "
          f"tape_replay_long\\PREREGISTRATION_LIVE_AB.md."]
    stamp = datetime.now(ET).strftime("%Y-%m-%d")
    path = os.path.join(a.dir, f"tape_ab_report_{stamp}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    with open(os.path.join(a.dir, f"tape_ab_report_{stamp}.json"), "w", encoding="utf-8") as f:
        json.dump({"verdict": verdict, "days": n_days, "effect_per_day": eff,
                   "ci90": ci, "shortfall_4h": short, "pause": len(p), "control": len(c),
                   "daily": ctx["daily"], "post_s": POST_S, "reward_x": REWARD_X,
                   "stop_usd": STOP_USD, "min_days": MIN_DAYS}, f, indent=1)
    print(path)
    print(f"verdict: {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
