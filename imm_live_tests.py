#!/usr/bin/env python3
r"""imm_live_tests.py -- the "Live A/B tests" block of the 7am portfolio
email (Jack 2026-11-05: "in the daily portfolio email, add section on live
A/B and results right after the risk controls. make it clear and succinct
and visually easy to digest").

One card per randomized test running on the IMM book, results through
yesterday:

  tape trigger A/B   IMM_TAPE_TRIGGER=ab: a coin flip pauses half the windows
                     a 100+ lot opens. imm_tape_ab_report.build scores it by
                     UTC day, as tape_replay_long\PREREGISTRATION_LIVE_AB.md
                     counts (its own email: "KL imm tape-ab", 08:10 ET).
  base size split    x1.5 on the "test" half of the series.
                     imm_size_test_report.score, by ET day, each half against
                     its own 14 days before (family watch pass 7 emails it).

Each card: the status (RUNNING to the test's last day, then the verdict its
own rule set in advance), the day of the test, the result in $/day with its
range drawn across $0, yesterday, the stop line and what comes next. The
result is coloured only when its range clears $0. A card shows from the
test's first scored day until SHOW_AFTER_END_DAYS after its last.

Read-only: no Kalshi call, nothing written. send_imm_digest builds it in its
--section-out child, which imports the bot already (the size test's halves
need it), and hands it over as ab_text / ab_html; send_portfolio_digest puts
it right under the risk block.
"""
import json
import os
from datetime import date, datetime, timedelta, timezone

import imm_tape_ab_report as tape_ab
from imm_risk_controls import AMBER, GREEN, RED, _bar_html, _esc

SHOW_AFTER_END_DAYS = 3          # a finished test's verdict shows this many mornings
TAPE_SINCE = date(2026, 11, 2)   # the tape A/B was built that day: nothing before it
TAPE_QUIET_DAYS = 7              # no tape window in this many days: the A/B is off
NEAR_STOP = 0.5                  # this far to its stop line, a test is flagged

INK, MUTED, TRACK = "#222", "#888", "#e3e3e3"
PILL = {"RUNNING": ("#1f5f8b", "#e8f1f8"),
        "GO": (GREEN, "#eafaf1"), "KEEP": (GREEN, "#eafaf1"),
        "WATCH": (AMBER, "#fef5e7"), "INCONCLUSIVE": (AMBER, "#fef5e7"),
        "STOP": (RED, "#fdecea"), "STOPPED": (RED, "#fdecea"), "NO-GO": (RED, "#fdecea")}
BAD = ("STOP", "STOPPED", "NO-GO")

TAPE_NEXT = {
    "RUNNING": "Keep running. GO needs day {of} ({ends}), the 4 h range above $0 "
               "and 5 min not negative.",
    "STOP": "The paused windows are past the stop line. Back to dry "
            "(IMM_TAPE_TRIGGER=dry, restart_imm.ps1 -Task) is your call.",
    "GO": "Pausing pays. Pausing every window (IMM_TAPE_AB_FRAC=1) is your call.",
    "NO-GO": "Pausing doesn't pay. Back to dry (IMM_TAPE_TRIGGER=dry) is your call.",
    "INCONCLUSIVE": "No clear answer at day {of}: run on, or stop. Your call.",
}
SIZE_NEXT = {
    "RUNNING": "Final read after {ends}: KEEP if net is over 1 SE above $0, STOP if "
               "over 1 SE below, else WATCH. It reads {read} today.",
    "STOPPED": "The stop rule ended it: every series is back at x1.",
    "KEEP": "x{mult:g} paid. The test half went back to x1 when the test ended; "
            "sizing the book up is your call.",
    "WATCH": "No clear answer in {of} days; the test half is back at x1. Re-run or "
             "leave it: your call.",
    "STOP": "x{mult:g} cost money; the test half is back at x1. Nothing to do.",
}


def usd(x) -> str:
    """Whole dollars, signed: +$34, -$4, $0."""
    if x is None:
        return "n/a"
    r = int(round(x))
    return "$0" if r == 0 else ("-" if r < 0 else "+") + f"${abs(r):,}"


def _md(d) -> str:
    return f"{d:%a} {d.month}/{d.day}"


def read_of(lo, hi, arm: str):
    """(words, colour) for a result's range: clear of $0 either way, or not."""
    if lo is not None and lo > 0:
        return f"{arm} ahead", GREEN
    if hi is not None and hi < 0:
        return f"{arm} behind", RED
    return "can't tell yet", MUTED


def _error_card(name: str, e: Exception) -> dict:
    return {"name": name, "error": f"{type(e).__name__}: {e}"}


# ---- the tests ------------------------------------------------------------

def tape_card(status_dir: str, now_utc: datetime, ctx=None):
    """The tape trigger A/B's card, scored through yesterday (UTC), or None
    when the A/B has no window in TAPE_QUIET_DAYS or ended over
    SHOW_AFTER_END_DAYS ago. `ctx` (tests): imm_tape_ab_report.build's."""
    until = now_utc.date() - timedelta(days=1)
    if ctx is None:
        ctx = tape_ab.build(status_dir, TAPE_SINCE, until)
    wins = ctx.get("wins") or []
    if not wins or not ctx.get("daily"):
        return None
    if not any(tape_ab._day(w["at"]) > until - timedelta(days=TAPE_QUIET_DAYS) for w in wins):
        return None
    of = tape_ab.MIN_DAYS
    if ctx["n_days"] > of + SHOW_AFTER_END_DAYS:
        return None
    ends = ctx["first"] + timedelta(days=of - 1)
    p, c = len(ctx["p"]), len(ctx["c"])
    eff, ci, short = ctx["eff"], ctx["ci"], ctx["short"]
    est = eff["4h"] if eff else None
    lo, hi = (ci["4h"] if ci and eff else (None, None))
    try:                          # the bot's own knobs, where the bot is importable
        import incentive_mm as imm
        hold, min_ct = imm.TAPE_HOLD_SECS, imm.TAPE_AB_MIN_CT
    except Exception:
        hold, min_ct = 60.0, 100.0
    y = ctx["daily"][-1]
    if y["pause"] == 0 and y["control"] == 0:
        y_val, y_sub = "none", "no windows"
    elif y["impact_4h"] is None:
        y_val, y_sub = "n/a", f"{y['pause']} paused / {y['control']} controls"
    else:
        y_val, y_sub = usd(y["impact_4h"]), f"{y['pause']} paused / {y['control']} controls"
    status = ctx["verdict"]
    return {
        "name": "Tape trigger A/B", "status": status, "day": ctx["n_days"], "of": of,
        "ends": ends,
        "what": (f"Pulls our quotes in an event for {hold:.0f} s after a {min_ct:.0f}+ lot "
                 f"sweeps it, on a coin-flip half of the windows ({p:,} paused, "
                 f"{c:,} controls so far)."),
        "est": est, "lo": lo, "hi": hi, "arm": "pausing",
        "est_note": "if every window paused (4 h marks, net of the reward given up)",
        "range_note": "90%",
        "facts": [
            {"label": "Yesterday", "value": y_val, "sub": y_sub},
            {"label": "So far", "value": usd(short), "sub": f"stop at -${tape_ab.STOP_USD:,.0f}",
             "bar": None if short is None else max(0.0, -short) / tape_ab.STOP_USD},
            {"label": "5 min check", "value": usd(eff["5m"]) + "/day" if eff else "n/a",
             "sub": "GO needs $0 or more"},
        ],
        "stop_frac": None if short is None else max(0.0, -short) / tape_ab.STOP_USD,
        "next": TAPE_NEXT[status].format(of=of, ends=_md(ends)),
    }


def load_dashboard(status_dir: str) -> dict:
    """The IMM dashboard's data object (its `const D = {...}`)."""
    with open(os.path.join(status_dir, "dashboard", "imm_dashboard.html"),
              encoding="utf-8") as f:
        html = f.read()
    i = html.index("const D = ") + len("const D = ")
    return json.JSONDecoder().raw_decode(html, i)[0]


def size_card(status_dir: str, now_utc: datetime, D=None):
    """The base size split test's card, or None outside its window (from its
    first complete ET day to SHOW_AFTER_END_DAYS after its end). `D` is the
    IMM dashboard's data object, read from status_dir when not given."""
    import imm_size_test_report as st
    imm = st.imm
    start, end = imm.size_test_window()
    now = now_utc.timestamp()
    if start is None or now < start + 86400 or now >= end + SHOW_AFTER_END_DAYS * 86400:
        return None
    if D is None:
        D = load_dashboard(status_dir)
    sc = st.score(D, now_utc.astimezone(imm.ET).date())
    if sc is None:
        return None
    n, of = len(sc["test_days"]), sc["n_days"]
    stopped = os.path.exists(os.path.join(status_dir, imm.SIZE_TEST_STOP_FILE))
    read = st.verdict(sc)
    status = "STOPPED" if stopped else ("RUNNING" if n < of else read)
    net, rew, pnl = sc["net"], sc["rew"], sc["pnl"]
    ends = imm.SIZE_TEST_FROM + timedelta(days=of - 1)
    last = datetime.strptime(sc["test_days"][-1], "%Y-%m-%d").date()
    yday = now_utc.astimezone(imm.ET).date() - timedelta(days=1)
    stop_avg, mult = sc["stop_avg"], imm.SIZE_TEST_MULT
    return {
        "name": f"Base size x{mult:g} split test", "status": status, "day": n, "of": of,
        "ends": ends,
        "what": (f"x{mult:g} base size on a random half of the series "
                 f"({sc['test']['series']}) vs the other half ({sc['control']['series']}), "
                 f"each against its own {len(sc['base_days'])} days before."),
        "est": net["mean"], "lo": net["mean"] - net["se"], "hi": net["mean"] + net["se"],
        "arm": f"x{mult:g}",
        "est_note": "net: the test half minus the control half",
        "range_note": "+/- 1 SE",
        "facts": [
            {"label": "Yesterday" if last == yday else f"Last day ({last.month}/{last.day})",
             "value": usd(net["daily"][-1]), "sub": "net"},
            {"label": "Last 3 days", "value": usd(stop_avg) + "/day" if stop_avg is not None
             else "n/a", "sub": f"stop below -${st.STOP_USD:,.0f}/day",
             "bar": None if stop_avg is None else max(0.0, -stop_avg) / st.STOP_USD},
            {"label": "Rewards / fills", "value": f"{usd(rew['mean'])} / {usd(pnl['mean'])}",
             "sub": "a day, test minus control"},
        ],
        "stop_frac": None if stop_avg is None else max(0.0, -stop_avg) / st.STOP_USD,
        "next": SIZE_NEXT[status].format(ends=_md(ends), read=read, mult=mult, of=of),
    }


def collect(status_dir: str, now_utc: datetime, D=None) -> list:
    """The cards of the tests running now, tape A/B first. A test that fails
    to score gets a one-line error card; nothing here raises. The size test
    is scored first so its dashboard read is let go before the tape scorer
    reads the dashboard's mark-outs."""
    try:
        size = size_card(status_dir, now_utc, D)
    except Exception as e:
        size = _error_card("Base size split test", e)
    D = None
    try:
        tape = tape_card(status_dir, now_utc)
    except Exception as e:
        tape = _error_card("Tape trigger A/B", e)
    return [c for c in (tape, size) if c]


# ---- rendering ------------------------------------------------------------

def summary(cards: list):
    """(words, colour) for the header: how many run, and the worst flag."""
    ok = [c for c in cards if not c.get("error")]
    bad = [f"{c['name']} {c['status']}" for c in ok if c["status"] in BAD]
    near = [c["name"] for c in ok if c["status"] not in BAD
            and (c.get("stop_frac") or 0) >= NEAR_STOP]
    running, n = sum(1 for c in ok if c["status"] == "RUNNING"), len(cards)
    head = f"{n} running" if running == n else f"{n} test" + ("" if n == 1 else "s")
    if bad:
        return f"{head}; {', '.join(bad)}", RED
    if near:
        return f"{head}; near the stop line: {', '.join(near)}", AMBER
    return f"{head}, none near its stop line", MUTED


def text_lines(cards: list) -> list:
    """The plain-text block (ASCII: the task console is cp1252)."""
    if not cards:
        return []
    words, _col = summary(cards)
    L = [f"LIVE A/B TESTS -- results through yesterday; {words}"]
    for c in cards:
        if c.get("error"):
            L.append(f"* {c['name']}: not available ({c['error']})")
            continue
        L.append(f"* {c['name']}: {c['status']}, day {c['day']} of {c['of']} "
                 f"(ends {_md(c['ends'])})")
        L.append(f"    {c['what']}")
        if c["est"] is not None:
            rd, _ = read_of(c["lo"], c["hi"], c["arm"])
            L.append(f"    Result: {usd(c['est'])}/day {c['est_note']}; range "
                     f"{usd(c['lo'])} to {usd(c['hi'])} ({c['range_note']}): {rd}")
        L.append("    " + " | ".join(f"{f['label']} {f['value']} ({f['sub']})"
                                     for f in c["facts"]))
        L.append(f"    Next: {c['next']}")
    return L


def _pill(status: str) -> str:
    col, bg = PILL.get(status, ("#555", "#eee"))
    return (f'<span style="display:inline-block;padding:1px 8px;border-radius:9px;'
            f'background:{bg};color:{col};font-size:11px;font-weight:700;'
            f'letter-spacing:.05em">{_esc(status)}</span>')


def _cells(runs, h: int) -> str:
    """A one-row bar from [(width px, colour)]."""
    cell = f"height:{h}px;line-height:{h}px;font-size:0;padding:0;"
    return ('<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;'
            'display:inline-table;vertical-align:middle"><tr>'
            + "".join(f'<td style="{cell}width:{w}px;background:{col}"></td>'
                      for w, col in runs if w > 0) + "</tr></table>")


def _progress_html(day: int, of: int, width: int = 60) -> str:
    w = int(round(min(1.0, day / float(of or 1)) * width))
    return _cells([(w, "#5b8db8"), (width - w, TRACK)], 6)


def range_runs(lo: float, est: float, hi: float, width: int = 140) -> list:
    """[(px, colour)] of a range bar on a scale centred on $0: the range in a
    green tint right of $0 and a red tint left of it, a 1px tick at $0 and a
    3px mark at the estimate, the rest grey."""
    m = max(abs(lo), abs(hi), abs(est), 1.0) * 1.1

    def px(x):
        return int(round((x + m) / (2.0 * m) * (width - 1)))

    a, b, z, e = px(lo), px(hi), px(0.0), px(est)
    mark = GREEN if lo > 0 else RED if hi < 0 else "#444"

    def colour(x):
        if e - 1 <= x <= e + 1:
            return mark
        if x == z:
            return "#666"
        if a <= x <= b:
            return "#b7e1c6" if x > z else "#f3c1bb"
        return "#eeeeee"

    runs = []
    for x in range(width):
        col = colour(x)
        if runs and runs[-1][1] == col:
            runs[-1][0] += 1
        else:
            runs.append([1, col])
    return [tuple(r) for r in runs]


def _card_html(c: dict) -> str:
    small = f"font-size:11px;color:{MUTED}"
    if c.get("error"):
        return (f'<div style="{small};margin:4px 0"><b>{_esc(c["name"])}</b>: not available '
                f'({_esc(c["error"])})</div>')
    h = [f'<div style="border:1px solid #e6e6e6;border-radius:6px;padding:8px 12px;'
         f'margin:6px 0;font-size:13px;line-height:1.45">']
    h.append(f'<div><b style="font-size:14px">{_esc(c["name"])}</b> &nbsp;{_pill(c["status"])}'
             f' &nbsp;<span style="{small}">day <b style="color:#555">{c["day"]}</b> of '
             f'{c["of"]}&nbsp; </span>{_progress_html(c["day"], c["of"])}'
             f'<span style="{small}">&nbsp; ends {_esc(_md(c["ends"]))}</span></div>')
    h.append(f'<div style="{small};margin:1px 0 6px">{_esc(c["what"])}</div>')
    # inline blocks, not table cells: on a phone they stack instead of
    # running off the screen
    box = "display:inline-block;vertical-align:top;margin:0 22px 4px 0"
    if c["est"] is not None:
        rd, rcol = read_of(c["lo"], c["hi"], c["arm"])
        big = INK if rcol == MUTED else rcol
        h.append(f'<div style="{box};max-width:330px"><span style="font-size:20px;'
                 f'font-weight:700;color:{big}">{usd(c["est"])}</span><span style="'
                 f'font-size:12px;color:#666">/day</span><div style="{small}">'
                 f'{_esc(c["est_note"])}</div></div>')
        h.append(f'<div style="{box};margin-top:6px;white-space:nowrap">'
                 f'<span style="{small}">{usd(c["lo"])}&nbsp;</span>'
                 + _cells(range_runs(c["lo"], c["est"], c["hi"]), 10)
                 + f'<span style="{small}">&nbsp;{usd(c["hi"])}</span>'
                 f'<div style="{small}">{_esc(c["range_note"])} range: '
                 f'<b style="color:{rcol}">{_esc(rd)}</b></div></div>')
    h.append('<div style="margin-top:3px">' + "".join(
        f'<div style="{box}"><div style="font-size:10px;color:{MUTED};font-weight:700;'
        f'letter-spacing:.05em">{_esc(f["label"].upper())}</div>'
        + (_bar_html(f["bar"]) if f.get("bar") else "")      # only once losing
        + f'<b>{_esc(f["value"])}</b> <span style="{small}">{_esc(f["sub"])}</span></div>'
        for f in c["facts"]) + "</div>")
    h.append(f'<div style="margin-top:5px"><b>Next:</b> {_esc(c["next"])}</div>')
    h.append("</div>")
    return "".join(h)


def html(cards: list) -> str:
    """The email block; "" when no test is running."""
    if not cards:
        return ""
    words, col = summary(cards)
    return ('<div style="font-size:15px;font-weight:600;margin:16px 0 4px">Live A/B tests '
            f'<span style="color:#888;font-weight:400;font-size:12px">results through '
            f'yesterday &middot; </span><span style="color:{col};font-weight:'
            f'{"600" if col != MUTED else "400"};font-size:12px">{_esc(words)}</span></div>'
            + "".join(_card_html(c) for c in cards))


def main(argv=None) -> int:
    """Print the block and write its HTML (a look by hand; sends nothing)."""
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="the Live A/B tests block, read-only")
    ap.add_argument("--dir", default=tape_ab.DEFAULT_DIR if hasattr(tape_ab, "DEFAULT_DIR")
                    else r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
    ap.add_argument("--html-out", help="also write the block's HTML here")
    a = ap.parse_args(argv)
    cards = collect(a.dir, datetime.now(timezone.utc))
    sys.stdout.write("\n".join(text_lines(cards) or ["(no live A/B test running)"]) + "\n")
    if a.html_out:
        with open(a.html_out, "w", encoding="utf-8") as f:
            f.write(html(cards))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
