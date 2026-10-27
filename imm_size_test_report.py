#!/usr/bin/env python3
"""IMM base size split test: the score, the stop rule, the emails (Jack
2026-10-22: "I want to test increasing base contract size by 1.5x" ->
"Split test").

incentive_mm quotes the "test" half of the series at SIZE_TEST_MULT for
SIZE_TEST_DAYS from 00:00 ET on SIZE_TEST_FROM (size_test_arm,
size_test_mult); the "control" half stays as it is. This scores it from the
IMM dashboard's data object -- per market per ET day, the reward estimate
and the trading P&L -- as a difference in differences: each completed test
day's test-half total minus the control half's, less the same gap averaged
over the BASELINE_DAYS before the test. The IMM family watch runs it as pass 7
once an ET day after RUN_HOUR_ET (yesterday is then complete):

- STOP RULE (set in advance): the latest 3 test days' net difference averages
  below -STOP_USD a day -> SIZE_TEST_STOP_FILE is written (the bot is back at
  1x within a minute) and "IMM size test STOPPED" is emailed.
- Emails at day 7 and day 14 (the end) with the verdict set in advance: KEEP
  when the net difference is more than one standard error above zero, STOP
  when more than one below, WATCH between (run longer to tell).
- SATURDAYS on their own (Jack 2026-10-26, after "Keep 2x for now" on
  Saturdays: "Add a Saturday split to the size-test report so 10/31 and 11/07
  show the x3 test half against the x2 control half in dollars"): the test's
  x1.5 rides on top of the Saturday multiplier, so on the long-dated series
  (the ones the Saturday mult sizes) the test half runs x3 against the
  control's x2. saturday_split scores those Saturdays the same way, against
  the pre-test SATURDAYS' gap, with each half's own dollars beside it.

  python imm_size_test_report.py      # print the score so far (no email, no stop file)
"""

import json
import math
import os
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import incentive_mm as imm

BASELINE_DAYS = 14
STOP_USD = float(os.environ.get("IMM_SIZE_TEST_STOP_USD", "400"))
STOP_RUN_DAYS = 3
RUN_HOUR_ET = 6
REPORT_DAYS = (7, 14)


def _day(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def arm_days(D: dict, member=None) -> Dict[str, Dict[str, List[float]]]:
    """{ET day: {arm: [rewards, trading P&L, series seen]}} over the
    dashboard's complete days ("ok"), for the two halves -- only the series
    `member(series)` accepts, when given."""
    F = {k: j for j, k in enumerate(D.get("day_fields") or [])}
    out: Dict[str, Dict[str, List[float]]] = {}
    for d, rec in sorted((D.get("days") or {}).items()):
        if not rec.get("ok", True):
            continue
        arms = {"test": [0.0, 0.0, set()], "control": [0.0, 0.0, set()]}
        for t, r in (rec.get("m") or {}).items():
            s = t.split("-")[0]
            if member is not None and not member(s):
                continue
            a = imm.size_test_arm(s)
            if a in arms:
                arms[a][0] += r[F["rew"]]
                arms[a][1] += r[F["pnl"]]
                arms[a][2].add(s)
        out[d] = {a: [v[0], v[1], len(v[2])] for a, v in arms.items()}
    return out


def _gap(rows: Dict[str, Dict[str, List[float]]], days: List[str], k: int) -> List[float]:
    """Test minus control on field k (0 rewards, 1 P&L, 2 = both: net)."""
    def val(a, d):
        v = rows[d][a]
        return v[0] + v[1] if k == 2 else v[k]
    return [val("test", d) - val("control", d) for d in days]


def score(D: dict, today_et: date) -> Optional[dict]:
    """The test so far, or None before its first complete day."""
    if imm.SIZE_TEST_FROM is None:
        return None
    start = imm.SIZE_TEST_FROM
    n_days = int(round(imm.SIZE_TEST_DAYS))
    rows = arm_days(D)
    test_days = [d for d in rows if start <= _day(d) < min(today_et, start + timedelta(days=n_days))]
    base_days = [d for d in rows if start - timedelta(days=BASELINE_DAYS) <= _day(d) < start]
    if not test_days or len(base_days) < BASELINE_DAYS // 2:
        return None
    out = {"test_days": test_days, "base_days": base_days, "n_days": n_days, "rows": rows}
    for name, k in (("rew", 0), ("pnl", 1), ("net", 2)):
        pre, post = _gap(rows, base_days, k), _gap(rows, test_days, k)
        base = statistics.mean(pre)
        did = [x - base for x in post]
        se = math.sqrt((statistics.pvariance(post) / len(post) if len(post) > 1 else 0.0)
                       + (statistics.pvariance(pre) / len(pre) if len(pre) > 1 else 0.0))
        out[name] = {"base_gap": base, "daily": did, "mean": statistics.mean(did), "se": se}
    for a in ("test", "control"):                    # each half against its own baseline
        b = [sum(rows[d][a][k] for d in base_days) / len(base_days) for k in (0, 1)]
        p = [sum(rows[d][a][k] for d in test_days) / len(test_days) for k in (0, 1)]
        out[a] = {"base": b, "test": p, "series": max(rows[d][a][2] for d in test_days)}
    last = out["net"]["daily"][-STOP_RUN_DAYS:]
    out["stop_avg"] = statistics.mean(last) if len(last) == STOP_RUN_DAYS else None
    out["sat"] = saturday_split(D, test_days, base_days)
    return out


def saturday_mult(d: str) -> float:
    """The Saturday multiplier the bot ran on ET date `d`: the gated step-up
    when the tracker's verdict covers it, else SAT_SIZE_MULT."""
    return imm.gated_sat_mult(_day(d)) or imm.SAT_SIZE_MULT


def saturday_split(D: dict, test_days: List[str], base_days: List[str]) -> Optional[dict]:
    """The test's Saturdays on the long-dated series only (the Saturday
    multiplier sizes them, so the test half runs SIZE_TEST_MULT x it): test
    half minus control half, less the same gap over the pre-test Saturdays,
    per Saturday for rewards / P&L / net, each half's own dollars, and each
    half's Saturday change against its pre-test Saturdays. None until a test
    Saturday is complete (or with no pre-test Saturday)."""
    sats = [d for d in test_days if _day(d).weekday() == 5]
    pre = [d for d in base_days if _day(d).weekday() == 5]
    if not sats or not pre:
        return None
    for load in ("load_sat_gate", "load_daily_series_file"):
        try:
            getattr(imm, load)()
        except Exception:
            pass
    rows = arm_days(D, member=lambda s: not imm.is_daily_series(s))
    out = {"saturdays": sats, "base_saturdays": pre,
           "mults": {d: (saturday_mult(d) * imm.SIZE_TEST_MULT, saturday_mult(d)) for d in sats},
           "halves": {d: {a: rows[d][a][:2] for a in ("test", "control")} for d in sats},
           "base_halves": {a: [statistics.mean(rows[d][a][k] for d in pre) for k in (0, 1)]
                           for a in ("test", "control")}}
    for name, k in (("rew", 0), ("pnl", 1), ("net", 2)):
        base = statistics.mean(_gap(rows, pre, k))
        did = [x - base for x in _gap(rows, sats, k)]
        out[name] = {"base_gap": base, "daily": did, "total": sum(did)}
    for a in ("test", "control"):
        out.setdefault("now_halves", {})[a] = [statistics.mean(rows[d][a][k] for d in sats) for k in (0, 1)]
    return out


def _sat_lift(sp: dict, k: int) -> Optional[float]:
    """The test half's Saturday change against its pre-test Saturdays,
    relative to the control half's (the Saturday version of _lift)."""
    t0, c0 = sp["base_halves"]["test"][k], sp["base_halves"]["control"][k]
    t1, c1 = sp["now_halves"]["test"][k], sp["now_halves"]["control"][k]
    try:
        return (t1 / t0) / (c1 / c0) - 1.0
    except ZeroDivisionError:
        return None


def saturday_lines(sp: Optional[dict]) -> List[str]:
    """The email's Saturday section (ASCII), or [] before a test Saturday."""
    if not sp:
        return []
    t_m, c_m = sp["mults"][sp["saturdays"][0]]
    out = [f"Saturdays, long-dated series (the test half at x{t_m:g} = Saturday x{c_m:g} x {imm.SIZE_TEST_MULT:g}, "
           f"the control at x{c_m:g}): test minus control, less their gap on the pre-test Saturdays "
           f"({', '.join(d[5:] for d in sp['base_saturdays'])}):"]
    for i, d in enumerate(sp["saturdays"]):
        h = sp["halves"][d]
        out.append(f"  {d[5:]}  rewards {_usd(sp['rew']['daily'][i])}, fills {_usd(sp['pnl']['daily'][i])}, "
                   f"net {_usd(sp['net']['daily'][i])}   (test half rewards ${h['test'][0]:,.0f} / fills "
                   f"{_usd(h['test'][1])}; control ${h['control'][0]:,.0f} / {_usd(h['control'][1])})")
    lr, lp = _sat_lift(sp, 0), _sat_lift(sp, 1)
    step = t_m / c_m
    el = lambda x: f" (elasticity {math.log(1 + x) / math.log(step):.2f})" \
        if x is not None and x > -1 and step > 1 else ""
    out.append(f"  Together: net {_usd(sp['net']['total'])}"
               + (f"; the x{t_m:g} half's Saturday rewards {lr * 100:+.0f}%{el(lr)}, fill P&L "
                  f"{lp * 100:+.0f}%{el(lp)} against x{c_m:g}" if lr is not None and lp is not None else "")
               + ". Positive = the extra Saturday size paid.")
    return out


def verdict(sc: dict) -> str:
    m, se = sc["net"]["mean"], sc["net"]["se"]
    if m - se > 0:
        return "KEEP"
    if m + se < 0:
        return "STOP"
    return "WATCH"


def _usd(v: float) -> str:
    return f"{'-' if v < 0 else '+'}${abs(v):,.0f}"


def _lift(sc: dict, k: int) -> Optional[float]:
    """The test half's change against its baseline, relative to the control
    half's: (test now / test before) / (control now / control before) - 1."""
    t, c = sc["test"], sc["control"]
    try:
        return (t["test"][k] / t["base"][k]) / (c["test"][k] / c["base"][k]) - 1.0
    except ZeroDivisionError:
        return None


def body(sc: dict, kind: str) -> Tuple[str, str]:
    """(subject, text) for a WEEK 1 / FINAL / STOPPED email."""
    v = "STOP" if kind == "STOPPED" else verdict(sc)
    n = len(sc["test_days"])
    net, rew, pnl = sc["net"], sc["rew"], sc["pnl"]
    label = {"WEEK 1": f"week 1 ({n} of {sc['n_days']} days)", "FINAL": f"final ({n} days)",
             "STOPPED": f"STOPPED after {n} day(s)"}[kind]
    subject = (f"IMM size test {label}: {v} -- net {_usd(net['mean'])}/day "
               f"(+/- ${net['se']:,.0f}), rewards {_usd(rew['mean'])}, fills {_usd(pnl['mean'])}")
    mult = imm.SIZE_TEST_MULT
    lr, lp = _lift(sc, 0), _lift(sc, 1)
    el = lambda x: f" (size elasticity {math.log(1 + x) / math.log(mult):.2f})" \
        if x is not None and x > -1 and mult > 1 else ""
    lines = [
        f"x{mult:g} base size on the test half ({sc['test']['series']} series) vs the "
        f"control half ({sc['control']['series']}), from {sc['test_days'][0]}. Each half "
        f"is measured against its own {len(sc['base_days'])} days before the test.",
        "",
        "Per day, test half minus control half, less their pre-test gap:",
        f"  Rewards  {_usd(rew['mean'])}  (+/- ${rew['se']:,.0f})",
        f"  Fills    {_usd(pnl['mean'])}  (+/- ${pnl['se']:,.0f})",
        f"  Net      {_usd(net['mean'])}  (+/- ${net['se']:,.0f}, one standard error)",
        "",
        (f"Test half vs its baseline, relative to control: rewards "
         f"{lr * 100:+.0f}%{el(lr)}, fill P&L {lp * 100:+.0f}%{el(lp)}. "
         f"The 10/11 x2s measured 0.51 and 0.90." if lr is not None and lp is not None else ""),
        "",
        f"Verdict (set in advance): KEEP when net is more than one standard error above "
        f"zero, STOP when more than one below, WATCH in between (run longer to tell).",
        f"Stop rule: the last {STOP_RUN_DAYS} days' net averaging below -${STOP_USD:,.0f}/day "
        + ("STOPPED the test: the bot is back at 1x." if kind == "STOPPED" else
           f"(now {_usd(sc['stop_avg'])}/day)." if sc["stop_avg"] is not None else "(not 3 days yet)."),
        "",
        "Day by day, net: " + ", ".join(f"{d[5:]} {_usd(x)}" for d, x in
                                       zip(sc["test_days"], net["daily"])),
        "",
        *(saturday_lines(sc.get("sat")) + ([""] if sc.get("sat") else [])),
        "Rewards are the dashboard's reward estimate; fills are trading P&L marked to each "
        "day's close. imm_size_test_report.py, IMM family watch pass 7.",
    ]
    return subject, "\n".join(x for x in lines if x is not None)


def stop_path() -> str:
    return os.path.join(imm.STATUS_DIR, imm.SIZE_TEST_STOP_FILE)


def due(st: dict, now: datetime) -> bool:
    """Once an ET day from RUN_HOUR_ET, after the first test day is complete,
    until the final (or stop) email has gone."""
    et = now.astimezone(imm.ET)
    start, _end = imm.size_test_window()
    sent = st.get("sent") or {}
    return (start is not None and now.timestamp() >= start + 86400 and et.hour >= RUN_HOUR_ET
            and st.get("day") != f"{et:%Y-%m-%d}" and "final" not in sent and "stop" not in sent)


def run(D: Optional[dict], st: dict, dry: bool, now: Optional[datetime] = None
        ) -> Tuple[str, Optional[Tuple[str, str]]]:
    """One pass: (summary, (subject, text) to email or None). `st` is the
    watch state's "size_test" entry, updated in place."""
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(imm.ET)
    if not due(st, now) or not D:
        return "", None
    sc = score(D, et.date())
    if sc is None:
        return "size test: no complete test day in the dashboard yet", None
    st["day"] = f"{et:%Y-%m-%d}"
    n = len(sc["test_days"])
    line = (f"size test: day {n}, net {_usd(sc['net']['mean'])}/day (+/- ${sc['net']['se']:,.0f}), "
            f"rewards {_usd(sc['rew']['mean'])}, fills {_usd(sc['pnl']['mean'])}, "
            f"{STOP_RUN_DAYS}-day {_usd(sc['stop_avg']) if sc['stop_avg'] is not None else '-'}")
    sent = st.setdefault("sent", {})
    if sc["stop_avg"] is not None and sc["stop_avg"] < -STOP_USD and not os.path.exists(stop_path()) \
            and "stop" not in sent:
        if not dry:
            with open(stop_path(), "w", encoding="utf-8") as f:
                f.write(f"{now.isoformat()} stop rule: the last {STOP_RUN_DAYS} days' net "
                        f"difference averaged {_usd(sc['stop_avg'])}/day\n")
            sent["stop"] = now.timestamp()
        return line + ", STOP RULE TRIPPED", body(sc, "STOPPED")
    for k in reversed(REPORT_DAYS):                  # a late first run sends the latest one
        key = "final" if k == REPORT_DAYS[-1] else f"day{k}"
        if n >= k and key not in sent:
            if not dry:
                sent[key] = now.timestamp()
                if key == "final":
                    sent.update({f"day{j}": now.timestamp() for j in REPORT_DAYS[:-1]})
            return line, body(sc, "FINAL" if key == "final" else "WEEK 1")
    return line, None


def main(argv=None) -> int:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import imm_family_watch as fw
    D = fw.load_dashboard()
    et = datetime.now(timezone.utc).astimezone(imm.ET)
    sc = score(D or {}, et.date())
    if sc is None:
        start, _end = imm.size_test_window()
        print("size test: no complete test day yet"
              + (f" (starts {datetime.fromtimestamp(start, imm.ET):%Y-%m-%d %H:%M} ET)" if start else
                 " (off)"))
        return 0
    subject, text = body(sc, "FINAL" if len(sc["test_days"]) >= REPORT_DAYS[-1] else "WEEK 1")
    print(subject)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
