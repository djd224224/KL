#!/usr/bin/env python3
"""The family side leans report (Jack 2026-10-22: "Both leans now but 1.5x the
winning side and .75 the losing side. And send me email on its performance",
then "Also build lean for Econ and rates").

Since the bot's FAMILY_SIDE_LEANS deploy on 10/22 (dfc5282):
  ASK lean -- Rotten Tomatoes, Other mentions, Econ & rates: asks 1.5x, bids
    0.75x (their bids lost the fills in the 10/22 study);
  BID lean -- Commodities & shipping, Company KPIs, AI & tech, Carbon Arc
    consumer: bids 1.5x, asks 0.75x (their asks lost).
The report reads the IMM dashboard's data object (its `const D`: the IMM's
own per-market day rows and fills with mark-outs, 31 days back) and compares
each group's last BEFORE_DAYS full ET days before the switch with the test's
(from 10/23; 10/22, the switch day, is left out), against the CONTROL: every
other family but TRUMP mentions (Trump B is its own test):

  quoting net a day = rewards (modelled) + the day's own fills; carry (the
    positions already held, re-marked or settled) is shown apart;
  fills by side = contracts a day and cents a contract to settlement (or to
    the mark now, for an event still open).

Verdict per group, set before the test ran:
  KEEP   the winning side beat the losing side a contract in the test, and
         the group's quoting net moved at least as well as the control's,
         scaled to the group's size (its before rewards / the control's).
  STOP   both failed: drop the group's families from IMM_FAMILY_SIDE_LEANS.
  WATCH  anything else, or fewer than MIN_FILL_CT contracts a side or
         MIN_EVENTS events with fills in the test.
The base size split test (Mon 10/26, 14 days, half the series x1.5 by hash)
overlaps: both of its halves hold series of every family, so it moves the
groups and the control alike.

Writes run-logs/incentive-mm/side_lean_report_<ET date>.md and prints its
path. Read-only: no Kalshi call, nothing in the bot touched.

  python imm_side_lean_report.py [--dash PATH] [--out-dir DIR] [--today YYYY-MM-DD]
"""
import argparse
import collections
import json
import os
from datetime import datetime, timedelta, timezone

DASH = r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm\dashboard\imm_dashboard.html"
OUT_DIR = r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm"
SWITCH_DAY = "2026-10-22"            # ET day of the deploy: left out
BEFORE_DAYS = 14
GROUPS = {
    "ASK": {"label": "ASK lean (asks 1.5x, bids 0.75x)", "win": "ask", "lose": "bid",
            "families": ("Rotten Tomatoes", "Other mentions", "Econ & rates")},
    "BID": {"label": "BID lean (bids 1.5x, asks 0.75x)", "win": "bid", "lose": "ask",
            "families": ("Commodities & shipping", "Company KPIs", "AI & tech",
                         "Carbon Arc consumer")},
}
EXCLUDED = ("TRUMP mentions",)       # its own test (Trump B), out of the control
MIN_FILL_CT = 300                    # contracts a side in the test, else too few to call
MIN_EVENTS = 5


def load_dashboard(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        html = f.read()
    i = html.index("const D = ") + len("const D = ")
    return json.JSONDecoder().raw_decode(html[i:])[0]


def et_day(ts: float) -> str:
    """The ET date of a UTC timestamp (EDT to 11/01 2am, EST after)."""
    u = datetime.fromtimestamp(ts, timezone.utc)
    off = -4 if u < datetime(2026, 11, 1, 6, tzinfo=timezone.utc) else -5
    return (u + timedelta(hours=off)).date().isoformat()


def windows(D: dict, today: str):
    """(before days, test days): full ET days on the dashboard -- the last
    BEFORE_DAYS before the switch day, and every day after it before today."""
    days = sorted(d for d in (D.get("days") or {}) if d < today)
    return ([d for d in days if d < SWITCH_DAY][-BEFORE_DAYS:],
            [d for d in days if d > SWITCH_DAY])


def group_of(fam: str) -> str:
    for g, spec in GROUPS.items():
        if fam in spec["families"]:
            return g
    return "" if fam in EXCLUDED else "CONTROL"


def summarize(D: dict, today: str) -> dict:
    """{"by": key -> window -> Counter of $ totals and fill counts, "events":
    key -> window -> set}, key = a group ("ASK", "BID", "CONTROL") or a lean
    family ("fam:<name>")."""
    ix = {f: i for i, f in enumerate(D["day_fields"])}
    mk = D.get("markets") or {}
    before, test = windows(D, today)
    which = {d: "before" for d in before}
    which.update({d: "test" for d in test})
    by = collections.defaultdict(lambda: {"before": collections.Counter(),
                                          "test": collections.Counter()})
    evs = collections.defaultdict(lambda: {"before": set(), "test": set()})

    def keys(t):
        fam = (mk.get(t) or {}).get("fam") or "Other"
        g = group_of(fam)
        if not g:
            return ()
        return (g,) if g == "CONTROL" else (g, "fam:" + fam)

    for d, w in which.items():
        for t, v in ((D["days"].get(d) or {}).get("m") or {}).items():
            rew, pnl = float(v[ix["rew"]] or 0), float(v[ix["pnl"]] or 0)
            car = float(v[ix["cr"]] or 0) + float(v[ix["cs"]] or 0)
            for k in keys(t):
                c = by[k][w]
                c["rew"] += rew
                c["own"] += pnl - car
                c["carry"] += car
    for f in D.get("fills") or []:
        w = which.get(et_day(f["ts"]))
        if w is None or f.get("pad"):
            continue
        side = "bid" if f.get("side") == "bid" else "ask"
        for k in keys(f.get("t")):
            c = by[k][w]
            c[side + "_ct"] += float(f.get("n") or 0)
            if f.get("mtc") is not None:
                c[side + "_cn"] += float(f.get("n") or 0)
                c[side + "_usd"] += float(f.get("n") or 0) * float(f["mtc"]) / 100.0
            evs[k][w].add((mk.get(f.get("t")) or {}).get("ev") or f.get("t"))
    for k in by:
        for w, ds in (("before", before), ("test", test)):
            by[k][w]["days"] = len(ds)
            by[k][w]["events"] = len(evs[k][w])
    return {"by": by, "before": before, "test": test}


def per_day(c, k):
    return c[k] / c["days"] if c["days"] else 0.0


def cents(c, side):
    return 100.0 * c[side + "_usd"] / c[side + "_cn"] if c[side + "_cn"] else None


def quoting(c):
    return per_day(c, "rew") + per_day(c, "own")


def verdict(by: dict, g: str):
    """(KEEP | STOP | WATCH, why) for a group by the rule in the module doc."""
    spec, x, ctl = GROUPS[g], by[g], by["CONTROL"]
    win, lose = spec["win"], spec["lose"]
    if not x["test"]["days"] or not x["before"]["days"]:
        return "WATCH", "no full test day yet"
    if (x["test"][win + "_ct"] < MIN_FILL_CT or x["test"][lose + "_ct"] < MIN_FILL_CT
            or x["test"]["events"] < MIN_EVENTS):
        return "WATCH", (f"too few fills to call ({x['test']['bid_ct']:,.0f} bid / "
                         f"{x['test']['ask_ct']:,.0f} ask contracts on {x['test']['events']} "
                         f"events; the bar is {MIN_FILL_CT} a side on {MIN_EVENTS})")
    cw, cl = cents(x["test"], win), cents(x["test"], lose)
    premise = cw is not None and cl is not None and cw > cl
    scale = (x["before"]["rew"] / ctl["before"]["rew"]) if ctl["before"]["rew"] else 0.0
    did = (quoting(x["test"]) - quoting(x["before"])) - scale * (
        quoting(ctl["test"]) - quoting(ctl["before"]))
    sides = (f"{win}s {cw:+.1f}c vs {lose}s {cl:+.1f}c a contract"
             if cw is not None and cl is not None else "a side without scored fills")
    if premise and did >= 0:
        return "KEEP", f"{sides}, and its quoting net {did:+,.2f} $/day against the control"
    if not premise and did < 0:
        return "STOP", f"{sides}, and its quoting net {did:+,.2f} $/day against the control"
    return "WATCH", f"mixed: {sides}; quoting net {did:+,.2f} $/day against the control"


def report(D: dict, today: str) -> str:
    s = summarize(D, today)
    by, before, test = s["by"], s["before"], s["test"]
    v = {g: verdict(by, g) for g in GROUPS}
    md_ = lambda d: f"{int(d[5:7])}/{int(d[8:10])}"                     # noqa: E731
    rng = lambda ds: f"{md_(ds[0])} to {md_(ds[-1])} ({len(ds)} days)" if ds else "none"  # noqa: E731
    lines = [f"# IMM family side leans: ASK {v['ASK'][0]}, BID {v['BID'][0]}", ""]
    for g, spec in GROUPS.items():
        lines += [f"**{spec['label']}, {', '.join(spec['families'])}: {v[g][0]}** -- "
                  f"{v[g][1]}.", ""]
    lines += [f"Before {rng(before)}; test {rng(test)}; 10/22, the switch day, is left out. "
              "Control: every other family but TRUMP mentions (Trump B's own test).", "",
              "| $ a day | ASK before | ASK test | BID before | BID test | control before "
              "| control test |", "|---|---|---|---|---|---|---|"]
    cols = [("ASK", "before"), ("ASK", "test"), ("BID", "before"), ("BID", "test"),
            ("CONTROL", "before"), ("CONTROL", "test")]
    for label, ks in (("rewards", ("rew",)), ("own fills", ("own",)), ("carry", ("carry",)),
                      ("**quoting net** (rewards + own fills)", ("rew", "own")),
                      ("net incl. carry", ("rew", "own", "carry"))):
        lines.append(f"| {label} | " + " | ".join(
            f"{sum(per_day(by[k][w], x) for x in ks):+,.2f}" for k, w in cols) + " |")
    lines += ["", "| Fills | ASK before | ASK test | BID before | BID test | control before "
              "| control test |", "|---|---|---|---|---|---|---|"]
    for side in ("bid", "ask"):
        lines.append(f"| {side}s: contracts a day | " + " | ".join(
            f"{per_day(by[k][w], side + '_ct'):,.0f}" for k, w in cols) + " |")
        lines.append(f"| {side}s: c a contract to settlement | " + " | ".join(
            ("n/a" if cents(by[k][w], side) is None else f"{cents(by[k][w], side):+.1f}c")
            for k, w in cols) + " |")
    lines += ["", "| Family | lean | quoting net $/day before | test | winning side c/ct "
              "test | losing side c/ct test | contracts/day win:lose test |",
              "|---|---|---|---|---|---|---|"]
    for g, spec in GROUPS.items():
        for fam in spec["families"]:
            x = by.get("fam:" + fam)
            if x is None:
                lines.append(f"| {fam} | {g} | n/a | n/a | n/a | n/a | n/a |")
                continue
            cw, cl = cents(x["test"], spec["win"]), cents(x["test"], spec["lose"])
            lines.append(
                f"| {fam} | {g} | {quoting(x['before']):+,.2f} | {quoting(x['test']):+,.2f} | "
                f"{'n/a' if cw is None else f'{cw:+.1f}c'} | "
                f"{'n/a' if cl is None else f'{cl:+.1f}c'} | "
                f"{per_day(x['test'], spec['win'] + '_ct'):,.0f} : "
                f"{per_day(x['test'], spec['lose'] + '_ct'):,.0f} |")
    lines += ["",
              "Rewards are the IMM's model (its dashboard), fills its own, each scored to "
              "settlement or, for an event still open, to its mark now. KEEP needs the "
              "winning side to beat the losing side a contract AND the group's quoting net "
              "to move at least as well as the control's (scaled to the group's size); "
              "STOP is both failing; anything else is WATCH. The base size split test "
              "(from 10/26) moves groups and control alike. To revert a group: drop its "
              "families from IMM_FAMILY_SIDE_LEANS (empty turns every lean off) and "
              "restart_imm.ps1 -Task."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dash", default=DASH)
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--today", default=None, help="ET date to report as of (default: today)")
    a = ap.parse_args(argv)
    today = a.today or et_day(datetime.now(timezone.utc).timestamp())
    md = report(load_dashboard(a.dash), today)
    path = os.path.join(a.out_dir, f"side_lean_report_{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
