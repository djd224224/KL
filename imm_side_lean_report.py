#!/usr/bin/env python3
"""The side-sizing tests report: the family side leans and the event long-shot
cut (Jack 2026-10-22: "Both leans now but 1.5x the winning side and .75 the
losing side. And send me email on its performance", "Also build lean for
Econ and rates", then "1, and give me email report").

Since the 10/22 deploys:
  ASK lean (dfc5282) -- Rotten Tomatoes, Other mentions, Econ & rates: asks
    1.5x, bids 0.75x (their bids lost the fills in the 10/22 study);
  BID lean (dfc5282) -- Commodities & shipping, Company KPIs, AI & tech,
    Carbon Arc consumer: bids 1.5x, asks 0.75x (their asks lost);
  LONG-SHOT cut (0bfe831) -- Sports & awards, Politics & approval,
    Elections, Weather & quakes: the side that buys the cheap contract at
    0.75x -- bids while the YES mid is under 30c, asks while it is 70c+.
The report reads the IMM dashboard's data object (its `const D`: the IMM's
own per-market day rows and fills with mark-outs, 31 days back) and compares
each group's last BEFORE_DAYS full ET days before the switch with the test's
(from 10/23; 10/22, the switch day, is left out), against its CONTROL: the
rest of the book -- every other family (TRUMP mentions is out: Trump B is its
own test). The families in none of the tests alone made ~$46/day of rewards
before, too small to scale against; the other groups' side changes are
reallocations within their families, so the rest of the book stays a fair
read of the market's own drift:

  quoting net a day = rewards (modelled) + the day's own fills; carry (the
    positions already held, re-marked or settled) is shown apart;
  fills = contracts a day and cents a contract to settlement (or to the mark
    now, for an event still open) -- by side for the leans, and for the
    long-shot cut its CUT fills (bids at YES < 30c, asks at YES >= 70c)
    against the REST of the event families' fills.

Verdict per group, set before the tests ran:
  KEEP   the premise held in the test -- the lean's winning side beat its
         losing side a contract; the cut fills lost more a contract than
         the rest -- and the group's quoting net moved at least as well as
         its control's, scaled to the group's size (its before rewards / the
         control's).
  STOP   both failed: take the group out (IMM_FAMILY_SIDE_LEANS without its
         families; IMM_LONGSHOT_MULT=1 for the cut).
  WATCH  anything else, or fewer than MIN_FILL_CT contracts on a compared
         side or MIN_EVENTS events with fills in the test.
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
SWITCH_DAY = "2026-10-22"            # ET day of the deploys: left out
BEFORE_DAYS = 14
GROUPS = {
    "ASK": {"label": "ASK lean (asks 1.5x, bids 0.75x)", "win": "ask", "lose": "bid",
            "families": ("Rotten Tomatoes", "Other mentions", "Econ & rates")},
    "BID": {"label": "BID lean (bids 1.5x, asks 0.75x)", "win": "bid", "lose": "ask",
            "families": ("Commodities & shipping", "Company KPIs", "AI & tech",
                         "Carbon Arc consumer")},
    "LONG": {"label": "Long-shot cut (0.75x bids under 30c YES, asks at 70c+)",
             "families": ("Sports & awards", "Politics & approval", "Elections",
                          "Weather & quakes")},
}
LONGSHOT_BID_BELOW, LONGSHOT_ASK_FROM = 30.0, 70.0
EXCLUDED = ("TRUMP mentions",)       # its own test (Trump B), out of the control
MIN_FILL_CT = 300                    # contracts on a compared side in the test
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
    """The test group of a family; "" for none (or excluded)."""
    for g, spec in GROUPS.items():
        if fam in spec["families"]:
            return g
    return ""


def control(by: dict, g: str, w: str) -> dict:
    """Group g's control in window w: the whole book ("ALL", TRUMP mentions
    out) less the group -- a plain dict, negatives kept."""
    a, x = by["ALL"][w], by[g][w]
    out = {k: a.get(k, 0) - x.get(k, 0) for k in set(a) | set(x)}
    out["days"] = a.get("days", 0)
    return out


def is_cut(f: dict) -> bool:
    """A fill on the side the long-shot cut shrinks."""
    px = float(f.get("px") or 0)
    return ((f.get("side") == "bid" and px < LONGSHOT_BID_BELOW)
            or (f.get("side") != "bid" and px >= LONGSHOT_ASK_FROM))


def summarize(D: dict, today: str) -> dict:
    """{"by": key -> window -> Counter of $ totals and fill counts}, key = a
    group ("ASK", "BID", "LONG"), the whole book less TRUMP mentions
    ("ALL") or a tested family ("fam:<name>").
    Fill counters per side ("bid_ct", "bid_cn" scored contracts, "bid_usd")
    and, for every key, "cut_*" / "rest_*" by is_cut."""
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
        if fam in EXCLUDED:
            return ()
        g = group_of(fam)
        return ("ALL", g, "fam:" + fam) if g else ("ALL",)

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
        n = float(f.get("n") or 0)
        side = "bid" if f.get("side") == "bid" else "ask"
        for k in keys(f.get("t")):
            c = by[k][w]
            for part in (side, "cut" if is_cut(f) else "rest"):
                c[part + "_ct"] += n
                if f.get("mtc") is not None:
                    c[part + "_cn"] += n
                    c[part + "_usd"] += n * float(f["mtc"]) / 100.0
            evs[k][w].add((mk.get(f.get("t")) or {}).get("ev") or f.get("t"))
    for k in by:
        for w, ds in (("before", before), ("test", test)):
            by[k][w]["days"] = len(ds)
            by[k][w]["events"] = len(evs[k][w])
    return {"by": by, "before": before, "test": test}


def per_day(c, k):
    return c.get(k, 0) / c["days"] if c.get("days") else 0.0


def cents(c, part):
    return 100.0 * c.get(part + "_usd", 0) / c[part + "_cn"] if c.get(part + "_cn") else None


def quoting(c):
    return per_day(c, "rew") + per_day(c, "own")


def verdict(by: dict, g: str):
    """(KEEP | STOP | WATCH, why) for a group by the rule in the module doc."""
    spec, x = GROUPS[g], by[g]
    ctl = {w: control(by, g, w) for w in ("before", "test")}
    good, bad = (("rest", "cut") if g == "LONG" else (spec["win"], spec["lose"]))
    names = ({"rest": "the other fills", "cut": "the cut fills"} if g == "LONG"
             else {spec["win"]: spec["win"] + "s", spec["lose"]: spec["lose"] + "s"})
    if not x["test"]["days"] or not x["before"]["days"]:
        return "WATCH", "no full test day yet"
    if (x["test"][good + "_ct"] < MIN_FILL_CT or x["test"][bad + "_ct"] < MIN_FILL_CT
            or x["test"]["events"] < MIN_EVENTS):
        return "WATCH", (f"too few fills to call ({x['test'][good + '_ct']:,.0f} "
                         f"{names[good]} / {x['test'][bad + '_ct']:,.0f} {names[bad]} contracts "
                         f"on {x['test']['events']} events; the bar is {MIN_FILL_CT} a side "
                         f"on {MIN_EVENTS})")
    cg, cb = cents(x["test"], good), cents(x["test"], bad)
    premise = cg is not None and cb is not None and cg > cb
    scale = (x["before"]["rew"] / ctl["before"]["rew"]) if ctl["before"]["rew"] else 0.0
    did = (quoting(x["test"]) - quoting(x["before"])) - scale * (
        quoting(ctl["test"]) - quoting(ctl["before"]))
    sides = (f"{names[good]} {cg:+.1f}c vs {names[bad]} {cb:+.1f}c a contract"
             if cg is not None and cb is not None else "a side without scored fills")
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
    cc = lambda c, p: "n/a" if cents(c, p) is None else f"{cents(c, p):+.1f}c"  # noqa: E731
    lines = [f"# IMM side tests: ASK lean {v['ASK'][0]}, BID lean {v['BID'][0]}, "
             f"long-shot cut {v['LONG'][0]}", ""]
    for g, spec in GROUPS.items():
        lines += [f"**{spec['label']}, {', '.join(spec['families'])}: {v[g][0]}** -- "
                  f"{v[g][1]}.", ""]
    lines += [f"Before {rng(before)}; test {rng(test)}; 10/22, the switch day, is left out. "
              "Each group's control: the rest of the book (TRUMP mentions is out: Trump B "
              "is its own test).", "",
              "| $ a day | rewards before | test | own fills before | test | carry before "
              "| test | **quoting net before** | **test** |",
              "|---|---|---|---|---|---|---|---|---|"]
    for k, label in (("ASK", "ASK lean"), ("BID", "BID lean"), ("LONG", "long-shot cut"),
                     ("ALL", "whole book (but TRUMP mentions)")):
        x = by[k]
        lines.append(f"| {label} | " + " | ".join(
            f"{per_day(x[w], f):+,.2f}" for f in ("rew", "own", "carry") for w in ("before", "test"))
            + f" | **{quoting(x['before']):+,.2f}** | **{quoting(x['test']):+,.2f}** |")
    lines += ["", "| Fills | contracts a day before | test | c a contract to settlement before "
              "| test |", "|---|---|---|---|---|"]
    for k, label, parts in (("ASK", "ASK lean", ("ask", "bid")), ("BID", "BID lean", ("bid", "ask")),
                            ("LONG", "long-shot cut", ("cut", "rest")),
                            ("ALL", "whole book", ("bid", "ask"))):
        x = by[k]
        for p in parts:
            name = {"cut": "cut fills (bids <30c, asks 70c+)", "rest": "other fills"}.get(p, p + "s")
            lines.append(f"| {label}: {name} | {per_day(x['before'], p + '_ct'):,.0f} | "
                         f"{per_day(x['test'], p + '_ct'):,.0f} | {cc(x['before'], p)} | "
                         f"{cc(x['test'], p)} |")
    lines += ["", "| Family | test | quoting net $/day before | test | the good side c/ct test "
              "| the shrunk side c/ct test |", "|---|---|---|---|---|---|"]
    for g, spec in GROUPS.items():
        good, bad = (("rest", "cut") if g == "LONG" else (spec["win"], spec["lose"]))
        for fam in spec["families"]:
            x = by.get("fam:" + fam)
            if x is None:
                lines.append(f"| {fam} | {g} | n/a | n/a | n/a | n/a |")
                continue
            lines.append(f"| {fam} | {g} | {quoting(x['before']):+,.2f} | "
                         f"{quoting(x['test']):+,.2f} | {cc(x['test'], good)} | "
                         f"{cc(x['test'], bad)} |")
    lines += ["",
              "Rewards are the IMM's model (its dashboard), fills its own, each scored to "
              "settlement or, for an event still open, to its mark now. KEEP needs the "
              "premise to hold (a lean's winning side beats its losing side a contract; the "
              "long-shot cut's fills lose more than the rest) AND the group's quoting net to "
              "move at least as well as its control's (the rest of the book), scaled to the "
              "group's size; STOP is "
              "both failing; anything else is WATCH. The base size split test (from 10/26) "
              "moves groups and control alike. To revert: drop a lean's families from "
              "IMM_FAMILY_SIDE_LEANS (empty turns every lean off), IMM_LONGSHOT_MULT=1 for the "
              "cut, then restart_imm.ps1 -Task."]
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
