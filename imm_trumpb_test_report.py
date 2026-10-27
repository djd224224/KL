#!/usr/bin/env python3
"""The Trump mentions B test report (Jack 2026-10-22: "Half bids, 1.5x asks
on B. Keep main series same. Give me an email report once this test runs").

KXTRUMPMENTIONB quoted bids at half size and asks at 1.5x from the bot's
SIDE_MULT_SERIES deploy on 10/22 (ceb14fa); KXTRUMPMENTION, the control,
kept its sizes. The report reads the IMM dashboard's data object (its `const
D`: the IMM's own per-market day rows and its fills with mark-outs, 31 days
back) and compares each series' full ET days before the switch (to 10/21)
with the test's (10/23 to 10/26; 10/22 was the switch day). The test ends
10/26: from 10/27 both series quote half-size bids and 1x asks (Jack
2026-10-27: "do 1 and 3" -- halving the main series' bids -- then "Trump
mentions B -- 1x asks instead of 1.5"), so there is no control after it:

  quoting net a day = rewards (modelled) + the day's own fills; carry (the
  positions already held, re-marked or settled) is shown apart.
  fills by side = contracts a day and $ a contract to settlement (or to now,
  for an event still open).

Verdict, set before the test ran (the change against the control, both a day):
  KEEP   B's quoting net rose more than the main series' (or fell less), and
         B's asks still made money a contract.
  STOP   B fell behind the main series and its asks lost: back to 1x / 1x
         (IMM_SIDE_MULT_SERIES= and a restart).
  WATCH  anything else, or too few B fills or events to call.
Since 10/27 the verdict only scores the 10/23-10/26 test: the sizes it would
act on are already gone (both series at half bids, 1x asks).

Writes run-logs/incentive-mm/trumpb_test_report_<ET date>.md and prints its
path. Read-only: no Kalshi call, nothing in the bot touched.

  python imm_trumpb_test_report.py [--dash PATH] [--out-dir DIR] [--today YYYY-MM-DD]
"""
import argparse
import collections
import json
import os
from datetime import datetime, timedelta, timezone

DASH = r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm\dashboard\imm_dashboard.html"
OUT_DIR = r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm"
SERIES = ("KXTRUMPMENTIONB", "KXTRUMPMENTION")     # the test, the control
SWITCH_DAY = "2026-10-22"                           # ET day of the deploy: left out
END_DAY = "2026-10-27"            # both series sized alike from this ET day: it and after left out
MIN_FILL_CT = 100        # fewer B contracts on a side in the test: too few to call
MIN_EVENTS = 3           # fewer B events with test fills: too few to call
ET = timezone(timedelta(hours=-4))


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
    """(before days, test days): full ET days in the dashboard, the switch
    day, END_DAY on and today (not over) left out."""
    days = sorted(d for d in (D.get("days") or {}) if d < today)
    return ([d for d in days if d < SWITCH_DAY],
            [d for d in days if SWITCH_DAY < d < END_DAY])


def summarize(D: dict, today: str) -> dict:
    """series -> window -> {"days", "rew", "own", "carry", "bid_ct", "bid_usd",
    "ask_ct", "ask_usd", "events"} ($ totals; the caller divides)."""
    ix = {f: i for i, f in enumerate(D["day_fields"])}
    mk = D.get("markets") or {}
    before, test = windows(D, today)
    which = {d: "before" for d in before}
    which.update({d: "test" for d in test})
    out = {s: {w: collections.Counter() for w in ("before", "test")} for s in SERIES}
    ev_sets = {s: {w: set() for w in ("before", "test")} for s in SERIES}
    for d, w in which.items():
        for t, v in ((D["days"].get(d) or {}).get("m") or {}).items():
            s = (mk.get(t) or {}).get("ser")
            if s not in out:
                continue
            rew, pnl = float(v[ix["rew"]] or 0), float(v[ix["pnl"]] or 0)
            car = float(v[ix["cr"]] or 0) + float(v[ix["cs"]] or 0)
            c = out[s][w]
            c["rew"] += rew
            c["own"] += pnl - car
            c["carry"] += car
    for f in D.get("fills") or []:
        s = (mk.get(f.get("t")) or {}).get("ser")
        if s not in out:
            continue
        w = which.get(et_day(f["ts"]))
        if w is None:
            continue
        side = "bid" if f.get("side") == "bid" else "ask"
        c = out[s][w]
        c[side + "_ct"] += float(f.get("n") or 0)
        c[side + "_usd"] += float(f.get("n") or 0) * float(f.get("mtc") or 0) / 100.0
        ev_sets[s][w].add((mk.get(f.get("t")) or {}).get("ev") or f.get("t"))
    for s in SERIES:
        for w, days in (("before", before), ("test", test)):
            out[s][w]["days"] = len(days)
            out[s][w]["events"] = len(ev_sets[s][w])
    return {"by": out, "before": before, "test": test}


def per_day(c, k):
    return c[k] / c["days"] if c["days"] else 0.0


def cents(c, side):
    return 100.0 * c[side + "_usd"] / c[side + "_ct"] if c[side + "_ct"] else None


def verdict(by: dict):
    """(KEEP | STOP | WATCH, why) by the rule in the module doc."""
    b, m = by["KXTRUMPMENTIONB"], by["KXTRUMPMENTION"]
    if not b["test"]["days"] or not b["before"]["days"]:
        return "WATCH", "no full test day yet"
    if (b["test"]["bid_ct"] < MIN_FILL_CT or b["test"]["ask_ct"] < MIN_FILL_CT
            or b["test"]["events"] < MIN_EVENTS):
        return "WATCH", (f"too few B fills to call ({b['test']['bid_ct']:.0f} bid / "
                         f"{b['test']['ask_ct']:.0f} ask contracts on {b['test']['events']} "
                         f"events; the bar is {MIN_FILL_CT} a side on {MIN_EVENTS})")
    q = lambda c: per_day(c, "rew") + per_day(c, "own")
    did = (q(b["test"]) - q(b["before"])) - (q(m["test"]) - q(m["before"]))
    ask = cents(b["test"], "ask")
    if did >= 0 and ask is not None and ask >= 0:
        return "KEEP", f"B gained {did:+.2f} $/day on the main series and its asks made {ask:+.1f}c a contract"
    if did < 0 and ask is not None and ask < 0:
        return "STOP", f"B fell {did:+.2f} $/day behind the main series and its asks lost {ask:+.1f}c a contract"
    return "WATCH", (f"mixed: B vs the main series {did:+.2f} $/day, B's asks "
                     f"{'n/a' if ask is None else f'{ask:+.1f}c'} a contract")


def report(D: dict, today: str) -> str:
    s = summarize(D, today)
    by, before, test = s["by"], s["before"], s["test"]
    v, why = verdict(by)
    md_ = lambda d: f"{int(d[5:7])}/{int(d[8:10])}"
    rng = lambda ds: f"{md_(ds[0])} to {md_(ds[-1])} ({len(ds)} days)" if ds else "none"
    lines = [f"# Trump mentions B test: {v}", "",
             f"**{v}**: {why}.", "",
             "KXTRUMPMENTIONB quoted bids at half size and asks at 1.5x from 10/22 "
             "(Jack: \"Half bids, 1.5x asks on B. Keep main series same\"); "
             "KXTRUMPMENTION kept its sizes and was the control.", "",
             f"Before {rng(before)}; test {rng(test)}; 10/22, the switch day, is left out. "
             "The test ends 10/26: from 10/27 both series quote half-size bids and 1x asks "
             "(Jack: \"Trump mentions B -- 1x asks instead of 1.5\"), so the main series "
             "is no control after it.", "",
             "| $ a day | B before | B test | main before | main test |",
             "|---|---|---|---|---|"]
    rows = (("rewards", "rew"), ("own fills", "own"), ("carry", "carry"))
    get = lambda ser, w, k: per_day(by[ser][w], k)
    for label, k in rows:
        lines.append(f"| {label} | {get('KXTRUMPMENTIONB', 'before', k):+,.2f} | "
                     f"{get('KXTRUMPMENTIONB', 'test', k):+,.2f} | "
                     f"{get('KXTRUMPMENTION', 'before', k):+,.2f} | {get('KXTRUMPMENTION', 'test', k):+,.2f} |")
    for label, ks in (("quoting net (rewards + own fills)", ("rew", "own")),
                      ("net incl. carry", ("rew", "own", "carry"))):
        vals = [sum(get(ser, w, k) for k in ks) for ser in SERIES for w in ("before", "test")]
        lines.append(f"| **{label}** | " + " | ".join(f"**{x:+,.2f}**" for x in vals) + " |")
    lines += ["", "| Fills | B before | B test | main before | main test |", "|---|---|---|---|---|"]
    for side in ("bid", "ask"):
        for label, fn in ((f"{side}s: contracts a day", lambda c: f"{per_day(c, side + '_ct'):,.0f}"),
                          (f"{side}s: c a contract to settlement", lambda c: (
                              "n/a" if cents(c, side) is None else f"{cents(c, side):+.1f}c"))):
            lines.append(f"| {label} | " + " | ".join(fn(by[ser][w]) for ser in SERIES
                                                      for w in ("before", "test")) + " |")
    lines.append("| events with fills | " + " | ".join(
        str(by[ser][w]["events"]) for ser in SERIES for w in ("before", "test")) + " |")
    lines += ["",
              "Rewards are the IMM's model (its dashboard), fills its own, each scored to "
              "settlement or, for an event still open, to its mark now. KEEP needs B's "
              "quoting net to beat the main series' change and B's asks to stay "
              "profitable; STOP is both failing; anything else is WATCH. Both series "
              "have quoted half-size bids and 1x asks since 10/27 (incentive_mm "
              "SIDE_MULT_SERIES); IMM_SIDE_MULT_SERIES= (empty) and restart_imm.ps1 -Task "
              "put both back at 1x / 1x."]
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dash", default=DASH)
    ap.add_argument("--out-dir", default=OUT_DIR)
    ap.add_argument("--today", default=None, help="ET date to report as of (default: today)")
    a = ap.parse_args(argv)
    today = a.today or datetime.now(timezone.utc).astimezone(ET).date().isoformat()
    md = report(load_dashboard(a.dash), today)
    path = os.path.join(a.out_dir, f"trumpb_test_report_{today}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(md)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
