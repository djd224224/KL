#!/usr/bin/env python3
r"""send_tape_ab_report.py -- the daily email on the tape trigger's LIVE A/B
(Jack 2026-11-02: "daily email update sharing daily impact and cumulative").

The A/B (IMM_TAPE_TRIGGER=ab) pauses a random half of the windows a 100+ lot
opens and only logs the other half; imm_tape_ab_report.py scores them. THE
EMAIL, money first, through yesterday (UTC days, as the pre-registered rule
counts them):
  yesterday  what the day's pauses made against the day's controls, net of
             the reward they gave up, at 4 h (and 5 min), and what pausing
             every window that day would have made
  so far     the same since the first window, every day pooled (the stop
             rule's number), and the $/day if every window paused, with its
             90% range
  verdict    STOP / RUNNING / GO / NO-GO / INCONCLUSIVE, per
             tape_replay_long\PREREGISTRATION_LIVE_AB.md
  by day     one line a day: windows, the day's result, the running total
It sends nothing until the A/B has a window in the last QUIET_AFTER_DAYS
days, so it starts with the A/B and goes quiet a week after its last window.

SOURCES, read-only: the bot's ws_tape / fills / orders sinks and the IMM
dashboard (imm_tape_ab_report.build). It writes only its sent-marker.

USAGE:
  python send_tape_ab_report.py                     # the scheduled daily run
  python send_tape_ab_report.py --dry               # build + print, send nothing
  python send_tape_ab_report.py --test              # send now, ignore the marker
  python send_tape_ab_report.py --until 2026-11-05  # score through that UTC day
Scheduled daily as "KL imm tape-ab" (register_imm_tape_ab.ps1), 08:10 ET.
All console output is ASCII (the task console is cp1252).
"""
import argparse
import glob
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from html import escape as _esc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_comp_lead_monitor as lm  # noqa: E402  (imports imm_quote_gaps first)
import imm_tape_ab_report as ab  # noqa: E402

imm = lm.imm
log = lm.log

SINCE = date(2026, 11, 2)       # the A/B was built that day: nothing before it
QUIET_AFTER_DAYS = 7            # no window in this many days: send nothing

ACTION = {
    "STOP": "the paused windows are more than ${stop:,.0f} behind the controls. "
            "Back to dry: IMM_TAPE_TRIGGER=dry in run_incentive_mm.ps1, then "
            "restart_imm.ps1 -Task.",
    "RUNNING": "keep it running. A GO needs day {days}, the 4 h range above zero "
               "and the 5 min result not negative.",
    "GO": "pausing pays. Pausing every window (IMM_TAPE_AB_FRAC=1) is your call.",
    "NO-GO": "pausing does not pay. Back to dry: IMM_TAPE_TRIGGER=dry, then "
             "restart_imm.ps1 -Task.",
    "INCONCLUSIVE": "no clear answer at day {days}: run on, or stop. Your call.",
}


def usd(x):
    return "n/a" if x is None else ("-" if x < 0 else "+") + f"${abs(x):,.2f}"


def _md(day):
    return datetime.strptime(day, "%Y-%m-%d").strftime("%m/%d")


def action_text(ctx):
    return ACTION[ctx["verdict"]].format(stop=ab.STOP_USD, days=ab.MIN_DAYS)


def yesterday_text(row):
    if row is None or (row["pause"] == 0 and row["control"] == 0):
        return "YESTERDAY: no A/B windows (the trigger was off, or nothing tripped)."
    if row["impact_4h"] is None:
        return (f"YESTERDAY: {row['pause']} paused, {row['control']} controls: no "
                f"comparison for the day (one arm is empty).")
    return (f"YESTERDAY: {row['pause']} windows paused, {row['control']} controls. "
            f"The pauses made {usd(row['impact_4h'])} at 4 h ({usd(row['impact_5m'])} "
            f"at 5 min), after the {usd(row['forgone']).lstrip('+')} of reward they "
            f"gave up. Pausing every window would have made {usd(row['all_on_4h'])}.")


def so_far_text(ctx):
    eff, ci = ctx["eff"], ctx["ci"]
    if eff is None:
        return "SO FAR: not both arms yet, so no comparison."
    rows = ctx["daily"]
    return (f"SO FAR ({_md(rows[0]['day'])} to {_md(rows[-1]['day'])}): {len(ctx['p'])} "
            f"windows paused, {len(ctx['c'])} controls. The pauses made "
            f"{usd(ctx['short'])} at 4 h (the stop line is -${ab.STOP_USD:,.0f}). "
            f"Pausing every window would make {usd(eff['4h'])}/day at 4 h (90% range "
            f"{usd(ci['4h'][0])} to {usd(ci['4h'][1])}) and {usd(eff['5m'])}/day at 5 min.")


def checks_text(ctx):
    p, c, dry = ctx["p"], ctx["c"], ctx["dry_pause"]
    n = len(p) + len(c) + len(dry)
    unm = sum(w["unmarked"] for w in p + c)
    bad = ctx["bad_draws"]
    out = (f"CHECKS: {len(p) + len(dry)} of {n} windows drew pause"
           + (f" (expected {ctx['frac']:.0%})" if ctx["frac"] is not None else "") + "; "
           + ("every draw recomputes from the log" if not bad
              else f"{bad} draw(s) do NOT recompute from the log") + "; "
           + (f"{unm} fill(s) not marked yet" if unm else "every fill marked")
           + (f"; the paused events were dark {sum(w['dark_s'] for w in p) / len(p):.0f}s "
              f"on average" if p else ""))
    if dry:
        out += (f"; {len(dry)} pause window(s) ran dry (the sweep breaker was off) and "
                f"are left out")
    return out + "."


def render_text(ctx):
    rows = ctx["daily"]
    out = [f"Tape trigger live A/B through {ctx['until']:%a %m/%d} (UTC day): day "
           f"{ctx['n_days']} of {ab.MIN_DAYS}",
           "Each window a 100+ lot opens is either paused (our quotes in the event "
           "pulled for 60s) or left alone as a control, on a coin flip. 'The pauses "
           "made' = the paused windows' fills against the controls', after the "
           "reward the pauses gave up; + is money the pauses made.",
           "", yesterday_text(rows[-1] if rows else None), "", so_far_text(ctx), "",
           f"VERDICT: {ctx['verdict']} -- {action_text(ctx)}"]
    if rows:
        out += ["", "BY DAY (4 h; 'so far' pools every day, as the stop rule counts)"]
        for r in rows:
            out.append(f"  {_md(r['day'])}: {r['pause']} paused / {r['control']} controls, "
                       + (f"the pauses {usd(r['impact_4h'])}" if r["impact_4h"] is not None
                          else "no comparison")
                       + f"; so far {usd(r['so_far_4h'])}")
    out += ["", checks_text(ctx)]
    return "\n".join(out)


def render_html(ctx):
    rows = ctx["daily"]
    p_ = "<p style='margin:6px 0'>{}</p>".format
    parts = [p_(f"<b>Tape trigger live A/B through {ctx['until']:%a %m/%d} (UTC day): "
                f"day {ctx['n_days']} of {ab.MIN_DAYS}</b>"),
             p_(_esc(f"VERDICT: {ctx['verdict']} -- {action_text(ctx)}")),
             p_(_esc(yesterday_text(rows[-1] if rows else None))),
             p_(_esc(so_far_text(ctx)))]
    if rows:
        th = "style='text-align:right;padding:2px 8px;border-bottom:1px solid #999'"
        td = "style='text-align:right;padding:2px 8px'"
        head = "".join(f"<th {th}>{h}</th>" for h in
                       ("UTC day", "paused", "controls", "the pauses (4 h)", "5 min",
                        "if all paused (4 h)", "so far (4 h)"))
        body = "".join(
            "<tr>" + "".join(f"<td {td}>{_esc(v)}</td>" for v in (
                _md(r["day"]), str(r["pause"]), str(r["control"]),
                "" if r["impact_4h"] is None else usd(r["impact_4h"]),
                "" if r["impact_5m"] is None else usd(r["impact_5m"]),
                "" if r["all_on_4h"] is None else usd(r["all_on_4h"]),
                usd(r["so_far_4h"]))) + "</tr>"
            for r in rows)
        parts.append("<table style='border-collapse:collapse;margin:8px 0'>"
                     f"<tr>{head}</tr>{body}</table>")
        parts.append(p_(_esc("'So far' pools every day's windows, as the stop rule "
                             "counts; it is close to, not exactly, the sum of the days.")))
    parts.append(p_(_esc(checks_text(ctx))))
    parts.append(p_(_esc("Each window a 100+ lot opens is either paused (our quotes in "
                         "the event pulled for 60s) or left alone as a control, on a coin "
                         "flip; 'the pauses made' is net of the reward they gave up. Rule: "
                         "tape_replay_long\\PREREGISTRATION_LIVE_AB.md.")))
    return ("<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.4'>"
            + "".join(parts) + "</div>")


def subject_for(ctx):
    rows = ctx["daily"]
    y = rows[-1] if rows else None
    day = (f"{usd(y['impact_4h'])} yesterday" if y and y["impact_4h"] is not None
           else "no comparison yesterday")
    return (f"Tape A/B {ctx['until']:%m/%d}: {day}, {usd(ctx['short'])} so far "
            f"(day {ctx['n_days']}/{ab.MIN_DAYS}) | {ctx['verdict']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true", help="build and print only; no email")
    ap.add_argument("--until", help="last UTC day to score (YYYY-MM-DD); default yesterday")
    args = ap.parse_args(argv)
    lm.low_priority()
    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    until = (date.fromisoformat(args.until) if args.until
             else now_utc.date() - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_tape_ab_sent_{today_et}.marker")
    if not (args.test or args.dry or args.until) and os.path.exists(marker):
        log(f"tape A/B email already sent for {today_et}; exiting")
        return 0
    ctx = ab.build(imm.STATUS_DIR, SINCE, until)
    recent = [w for w in ctx["wins"]
              if ab._day(w["at"]) > until - timedelta(days=QUIET_AFTER_DAYS)]
    if not recent and not args.test:
        log(f"tape A/B email: no A/B window in the {QUIET_AFTER_DAYS} days to {until}; "
            f"nothing sent")
        return 0
    text, html, subject = render_text(ctx), render_html(ctx), subject_for(ctx)
    log("tape A/B email: " + subject + "\n" + text)
    if args.dry:
        return 0
    alerter = imm.Alerter("IMM-TAPEAB", live=True)
    if not alerter.enabled:
        log("cannot send tape A/B email: alert credentials not configured")
        return 1
    attempts = 1 if args.test else 8
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=("[TEST] " if args.test else "") + subject,
                                  html=html)
        if ok:
            break
        log(f"tape A/B send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"tape A/B send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.until):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR, "imm_tape_ab_sent_*.marker")):
            name = os.path.basename(old)[len("imm_tape_ab_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
