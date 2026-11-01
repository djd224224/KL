#!/usr/bin/env python3
"""imm_unquoted_weekly.py -- the weekly email of the paying series the IMM
does not quote. Jack 2026-11-01: "yes", on a weekly email of the unquoted
list.

Why weekly: the 30-min family watch (imm_family_watch.py) emails a newly
paying series once and then never again, and Kalshi paused most program
renewals around 10/28, so a series that grew after its alert, or comes back
paying, reaches Jack only here.

What it does: reads the dashboard's gap list (dashboard/cache/api_gaps.pkl,
the 7:20 quote-gaps classifier, refreshed hourly by the "KL imm dashboard"
task), scores every "not in allowlist" series with imm_unquoted -- the bot's
own estimator on the live books at x1 and x3, 75 days of public tape for
makers' P&L, Jack's calls (ADD / ADD WITH CARE / PASS / DON'T QUOTE) -- and
sends one short email ranked by the x1 $/day. A series worth adding (an ADD
/ ADD WITH CARE call on $1/day or more at x3) gets its title, call and
reason, x1 / x3 estimate, pool, tape, what adding it would take and what the
family watch already sent; the biggest passed ones get a line each; the
rest are named at the end. The series records and tapes
are cached beside the dashboard's (unquoted_tape.json), which the dashboard's
Promising unquoted series table reads too.

Scheduled weekly, Sundays 08:00 ET ("KL imm unquoted-weekly", registered by
register_imm_unquoted_weekly.ps1 -- Jack's to run). Idempotent per ET day
via a sent-marker.

  python imm_unquoted_weekly.py            # the scheduled run
  python imm_unquoted_weekly.py --dry      # build and print only: no email, no marker
  python imm_unquoted_weekly.py --test     # send now, whatever the marker says
"""

import argparse
import os
import pickle
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import imm_quote_gaps  # noqa: F401  (the launcher's env, then incentive_mm)
import incentive_mm as imm
import imm_dashboard as dash
import imm_unquoted as iu
from incentive_mm import log

PASSED_SHOW = 20              # passed series with a line of their own; the rest are named
GAPS_STALE_H = 6.0
MARKER = "imm_unquoted_weekly_sent_{day}.marker"


def gaps_path() -> str:
    return dash._api_cache_path("gaps")


def load_gaps(path: str):
    """(gap rows, built at epoch) from the dashboard's cache."""
    with open(path, "rb") as f:
        c = pickle.load(f)
    return (c.get("val") or {}).get("rows") or [], float(c.get("at") or 0.0)


def _et(ts: float, fmt: str = "%b %d %I:%M %p ET") -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(imm.ET).strftime(fmt) \
        .replace(" 0", " ")


def _usd(v: float) -> str:
    return f"${v:,.2f}" if v < 100 else f"${v:,.0f}"


def entry(i: int, r: dict) -> list:
    """A series worth adding: what it is, the call, the money, what adding it
    takes and what the watch sent."""
    head = f"{i}. {r['series']}: {r.get('title') or r['series']}"
    meta = ", ".join(x for x in (r.get("category"), r.get("frequency")) if x)
    lines = [head + (f" ({meta})" if meta else "")]
    lines.append(f"   {r['call']}: {r['why']}")
    money = (f"   x1 {_usd(r.get('x1', 0.0))}/day, x3 {_usd(r.get('x3', 0.0))}/day"
             + ("+" if r.get("partial") else "")
             + f"; pool {_usd(r['pool'])}/day; {r.get('x1_bar', 0)} of {r['n']} markets "
               f"clear the ${r.get('bar', 1.3):.2f} bar at x1, {r.get('x3_bar', 0)} at x3")
    tp = r.get("tape")
    if tp is not None and "makers" not in r["why"] and "tape" not in r["why"]:
        money += f"; {iu.tape_text(tp)}"
    lines.append(money)
    if r.get("needs"):
        lines.append(f"   Takes: {r['needs']}")
    lines.append(f"   Watch: {r['watch']}")
    return lines


def passed_line(r: dict) -> str:
    """A passed series in one line: what it is, the call and why, the money,
    and the watch's alert if it sent one."""
    return (f"- {r['series']}: {r.get('title') or r['series']} -- {r['call']}: {r['why']}. "
            f"x1 {_usd(r.get('x1', 0.0))}/day, x3 {_usd(r.get('x3', 0.0))}"
            + ("+" if r.get("partial") else "") + f", pool {_usd(r['pool'])}/day"
            + (f"; {r['watch']}" if r.get("watch_alerted") else ""))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def build_email(res: dict, gaps_at: float, now_ts: float):
    """(subject, body) from imm_unquoted.score_all's result: the series worth
    adding (imm_unquoted.promising) in full, then the biggest PASSED_SHOW
    passed ones worth $1/day at x3 on a line each, both ranked by the x1
    $/day as score_all orders them, then the rest by name."""
    rows = res["rows"]
    good = [r for r in rows if iu.promising(r)]
    passed = [r for r in rows if r not in good and r.get("x3", 0.0) >= iu.PROMISING_MIN_X3]
    small = [r for r in rows if r not in good and r not in passed and r.get("quotable")]
    dark = [r for r in rows if r not in good and r not in passed and not r.get("quotable")]
    n = Counter(r["call"] for r in good)
    x1 = sum(r.get("x1", 0.0) for r in good)
    x3 = sum(r.get("x3", 0.0) for r in good)
    if good:
        subject = ("IMM unquoted weekly: " + ", ".join(f"{n[c]} {c}" for c in iu.CALLS if n[c])
                   + f" (~${x1:,.0f}/day at x1, ~${x3:,.0f} at x3)")
    else:
        subject = f"IMM unquoted weekly: nothing to add ({len(rows)} series scored)"
    pool = sum(r["pool"] for r in rows)
    out = [f"Paying series the IMM does not quote: {len(rows)} series, ${pool:,.0f}/day of "
           f"pool. Books read {_et(now_ts)}; gap list from {_et(gaps_at)}"
           + (f" ({(now_ts - gaps_at) / 3600:.0f}h old: the dashboard's Kalshi reads "
              f"may be failing)" if now_ts - gaps_at > GAPS_STALE_H * 3600 else "") + ".",
           "x1 = adding it to extra_allow_series.json; x3 = a code override (as KXTRUMPVH). "
           f"Tape = makers' P&L to settlement over the last {iu.TAPE_DAYS} days of public trades.",
           ""]
    if good:
        out.append(f"WORTH ADDING ({len(good)}), by x1 $/day")
        for i, r in enumerate(good, 1):
            out += entry(i, r) + [""]
    else:
        out += ["Nothing is worth adding this week.", ""]
    if passed:
        out.append(f"PASSED, $1/day or more at x3 ({len(passed)}), by x1 $/day")
        out += [passed_line(r) for r in passed[:PASSED_SHOW]]
        if len(passed) > PASSED_SHOW:
            out.append(f"- also passed ({len(passed) - PASSED_SHOW}): " + ", ".join(
                r["series"] for r in passed[PASSED_SHOW:]))
        out.append("")
    if small:
        out.append(f"Under $1/day even at x3 ({len(small)}): " + ", ".join(
            f"{r['series']} {_usd(r.get('x3', 0.0))}"
            + (f" ({r['call']})" if r["call"] in iu.PROMISING_CALLS else "") for r in small))
    if dark:
        out.append(f"Nothing quotable now ({len(dark)}): " + ", ".join(
            r["series"] for r in dark))
    if res.get("enrolled"):
        out.append(f"Enrolled since the gap list was built, left out ({len(res['enrolled'])}): "
                   + ", ".join(res["enrolled"]))
    ctx = res.get("ctx") or {}
    out.append(f"({_plural(ctx.get('books', 0), 'book')} and "
               f"{_plural(ctx.get('tape_reads', 0), 'tape')} read in {ctx.get('secs', 0):.0f}s.)")
    return subject, "\n".join(out).rstrip() + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry", action="store_true", help="build and print only: no email, no marker")
    ap.add_argument("--test", action="store_true", help="send now, whatever the marker says")
    ap.add_argument("--gaps", default=None, help="the gap list (default: the dashboard's cache)")
    ap.add_argument("--max-books", type=int, default=None)
    ap.add_argument("--tape-budget", type=int, default=None,
                    help="series whose tape may be read (default: every one that needs it)")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    day = now.astimezone(imm.ET).date().isoformat()
    marker = os.path.join(imm.STATUS_DIR, MARKER.format(day=day))
    if not (args.dry or args.test) and os.path.exists(marker):
        log(f"[UNQUOTED] weekly email already sent for {day}; exiting")
        return 0
    attempts = 1 if (args.dry or args.test) else 3
    res = subject = body = None
    for attempt in range(1, attempts + 1):
        try:
            rows, gaps_at = load_gaps(args.gaps or gaps_path())
            res = iu.score_all(rows, now, max_books=args.max_books,
                               cache_path=os.path.join(dash.CACHE_DIR, iu.TAPE_CACHE_NAME),
                               tape_budget=args.tape_budget)
            subject, body = build_email(res, gaps_at, time.time())
            break
        except Exception as e:
            log(f"[UNQUOTED] build attempt {attempt}/{attempts} failed: {e!r}")
            if attempt == attempts:
                return 1
            time.sleep(300)
    log(f"[UNQUOTED] {subject}\n{body}")
    if args.dry:
        return 0
    alerter = imm.Alerter("IMM-UNQUOTED", live=True)
    if not alerter.enabled:
        log("[UNQUOTED] cannot send: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, (1 if args.test else 8) + 1):
        ok = alerter.send_message(body, subject=subject)
        if ok:
            break
        log(f"[UNQUOTED] send attempt {attempt} failed; retry in 5 min")
        time.sleep(300)
    log(f"[UNQUOTED] send: {'ok' if ok else 'FAILED'}")
    if ok and not args.test:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(now.isoformat())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
