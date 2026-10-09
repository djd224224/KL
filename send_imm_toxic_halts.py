#!/usr/bin/env python3
r"""send_imm_toxic_halts.py -- the daily "what did the toxic-flow halt stand
down yesterday" email (Jack 2026-09-29: "and give me a daily morning email on
what was halted in the prior day").

WHAT IT REPORTS, for the prior ET calendar day:
  * every EVENT halt (pick-offs on TOXIC_EVENT_MARKETS different markets of
    one event inside TOXIC_EVENT_WINDOW_SECS -> the whole event out for
    TOXIC_EVENT_HALT_SECS): which markets were picked off on which side, how
    many markets the halt took down, and the reward it forwent;
  * every SIDE halt (TOXIC_PICKOFFS pick-offs on one side of a market inside
    TOXIC_WINDOW_SECS -> that side out for TOXIC_HALT_SECS): the pick-offs
    that caused it (fill price -> mark 5 minutes later), the forgone reward,
    and what the price did DURING the halt -- still moving against the side
    means the halt kept the bot out of it; moving back means it cost rent;
  * the most picked-off markets, including pick-offs that never reached a
    halt.
The rule itself lives in incentive_mm.py (TOXIC_* knobs, _toxic_confirm).

SOURCES: the bot's own toxic_halts_<UTC date>.jsonl sink (kind = pickoff /
side_halt / event_halt, written as each one happens) and marks_<UTC
date>.jsonl (the 5-minute mark of every open position) for the price during
a halt. STRICTLY READ-ONLY: no API calls, no orders; the only file written
is the daily sent-marker. Imports imm_quote_gaps first, like the other
morning emails, so the launcher env and the alert credentials apply.

USAGE:
  python send_imm_toxic_halts.py                   # the scheduled daily run
  python send_imm_toxic_halts.py --dry             # build + print, send nothing
  python send_imm_toxic_halts.py --test            # send now, ignore the marker
  python send_imm_toxic_halts.py --day 2026-09-28  # a specific ET day
Scheduled daily as "KL imm toxic-halts" (register_imm_toxic_halts.ps1).
Sends every morning, including "nothing halted", so a silent day is
distinguishable from a broken task. All console output is ASCII (the task
console is cp1252).
"""
import argparse
import bisect
import glob
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape as _esc

# imm_quote_gaps FIRST: it mirrors the launcher's $ProbeEnv and the alert
# credential fallback into the environment before incentive_mm reads config.
import imm_quote_gaps as _gaps  # noqa: F401  (import side effects)
import incentive_mm as imm
from incentive_mm import log

MAX_ROWS = int(os.environ.get("IMM_TOXIC_EMAIL_MAX_ROWS", "60"))
TD = 'padding:4px 9px;border:1px solid #ddd;text-align:right;'
TDL = 'padding:4px 9px;border:1px solid #ddd;text-align:left;'


def day_window(day) -> tuple:
    """[00:00 ET day, 00:00 ET next day) as UTC datetimes."""
    start = imm.ET.localize(datetime(day.year, day.month, day.day))
    end = imm.ET.localize(datetime(day.year, day.month, day.day) + timedelta(days=1))
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _utc_dates(start: datetime, end: datetime) -> list:
    d, out = start.date(), []
    while d <= (end - timedelta(seconds=1)).date():
        out.append(d)
        d += timedelta(days=1)
    return out


def load_records(start: datetime, end: datetime, status_dir=None) -> tuple:
    """(records inside [start, end), whether any sink file existed)."""
    status_dir = status_dir or imm.STATUS_DIR
    lo, hi = start.timestamp(), end.timestamp()
    rows, seen_file = [], False
    for d in _utc_dates(start, end + timedelta(days=1)):
        path = os.path.join(status_dir, f"toxic_halts_{d:%Y-%m-%d}.jsonl")
        if not os.path.exists(path):
            continue
        seen_file = True
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if lo <= float(r.get("ts") or 0) < hi:
                    rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows, seen_file


def load_marks(tickers: set, start: datetime, end: datetime,
               status_dir=None) -> dict:
    """ticker -> sorted [(ts, mark_cents)] from the marks sink (open
    positions only, every ~5 min), over the window plus a day of spill."""
    status_dir = status_dir or imm.STATUS_DIR
    out = defaultdict(list)
    if not tickers:
        return out
    for d in _utc_dates(start, end + timedelta(days=1)):
        path = os.path.join(status_dir, f"marks_{d:%Y-%m-%d}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                i = line.find('"ticker": "')
                if i < 0:
                    continue
                t = line[i + 11:line.find('"', i + 11)]
                if t not in tickers:
                    continue
                try:
                    r = json.loads(line)
                    mk = r.get("mark_cents")
                    if mk is None:
                        continue
                    ts = datetime.fromisoformat(r["ts"]).timestamp()
                except (ValueError, KeyError):
                    continue
                out[t].append((ts, float(mk)))
    for t in out:
        out[t].sort()
    return out


def mark_at(marks: dict, t: str, ts: float, slack: float = 900.0):
    """First mark at/after ts within `slack` seconds, else None."""
    arr = marks.get(t) or []
    k = bisect.bisect_left(arr, (ts, -1e9))
    if k < len(arr) and arr[k][0] - ts <= slack:
        return arr[k][1]
    return None


def _et(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(imm.ET).strftime("%H:%M")


def _short(t: str, ev: str) -> str:
    return t[len(ev) + 1:] if t.startswith(ev + "-") else t


def build_report(day, status_dir=None) -> dict:
    start, end = day_window(day)
    rows, seen_file = load_records(start, end, status_dir)
    picks = [r for r in rows if r.get("kind") == "pickoff"]
    sides = [r for r in rows if r.get("kind") == "side_halt"]
    events = [r for r in rows if r.get("kind") == "event_halt"]
    marks = load_marks({r["ticker"] for r in sides}, start, end, status_dir)
    side_rows = []
    for h in sides:
        prior = [p for p in picks if p["ticker"] == h["ticker"]
                 and p["side"] == h["side"] and p["ts"] <= h["ts"]
                 and h["ts"] - p["ts"] <= float(h.get("window_secs") or 86400)]
        m0 = next((p["mark"] for p in reversed(prior)), None)
        m1 = mark_at(marks, h["ticker"], float(h["until"]))
        move = None
        if m0 is not None and m1 is not None:
            move = (m0 - m1) if h["side"] == "bid" else (m1 - m0)
        halt_s = float(h["until"]) - float(h["ts"])
        side_rows.append(dict(
            ts=h["ts"], ticker=h["ticker"], event=h.get("event") or "",
            side=h["side"], picks=[(p["fill_px"], p["mark"]) for p in prior],
            m0=m0, m1=m1, move=move, halt_min=halt_s / 60.0,
            forgone=float(h.get("est_per_day") or 0.0) * halt_s / 86400.0))
    event_rows = []
    for h in events:
        halt_s = float(h["until"]) - float(h["ts"])
        ev = h["event"]
        picked = []
        for t in h.get("markets_picked") or []:
            ps = [p for p in picks if p["ticker"] == t and p["ts"] <= h["ts"]
                  and h["ts"] - p["ts"] <= float(h.get("window_secs") or 3600)]
            side = ps[-1]["side"] if ps else "?"
            picked.append(f"{_short(t, ev)} ({side})")
        event_rows.append(dict(
            ts=h["ts"], event=ev, picked=picked,
            halted=int(h.get("markets_halted") or 0), halt_min=halt_s / 60.0,
            est=float(h.get("est_per_day") or 0.0),
            forgone=float(h.get("est_per_day") or 0.0) * halt_s / 86400.0))
    by_mkt = defaultdict(list)
    for p in picks:
        by_mkt[(p["ticker"], p["side"])].append(p)
    top = sorted(by_mkt.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:15]
    ctx = dict(
        day=day, seen_file=seen_file, n_picks=len(picks),
        n_pick_mkts=len({p["ticker"] for p in picks}),
        side_rows=side_rows, event_rows=event_rows,
        n_side_mkts=len({r["ticker"] for r in side_rows}),
        n_events=len({r["event"] for r in event_rows}),
        forgone=sum(r["forgone"] for r in side_rows + event_rows),
        side_hours=sum(r["halt_min"] for r in side_rows) / 60.0,
        top=[(t, s, ps) for (t, s), ps in top])
    return ctx


def subject_for(ctx) -> str:
    d = ctx["day"].strftime("%a %b %d")
    if not ctx["side_rows"] and not ctx["event_rows"]:
        return f"IMM toxic halts {d}: none ({ctx['n_picks']} pick-off(s))"
    return (f"IMM toxic halts {d}: {len(ctx['event_rows'])} event, "
            f"{len(ctx['side_rows'])} side ({ctx['n_picks']} pick-offs)")


def _rule_lines() -> list:
    lines = [
        f"Rule: a pick-off = one of our fills with the price "
        f"{imm.TOXIC_PICKOFF_CENTS:g}c+ against it "
        f"{imm.TOXIC_CONFIRM_SECS // 60} min later (pads/takers excluded).",
        f"  Event halt: pick-offs on {imm.TOXIC_EVENT_MARKETS} markets of one "
        f"event within {imm.TOXIC_EVENT_WINDOW_SECS / 60:g} min -> the whole "
        f"event off {imm.TOXIC_EVENT_HALT_SECS / 60:g} min.",
        f"  Side halt: {imm.TOXIC_PICKOFFS} pick-offs on one side of a market "
        f"within {imm.TOXIC_WINDOW_SECS / 3600:g}h -> that side off "
        f"{imm.TOXIC_HALT_SECS / 60:g} min."]
    if imm.TOXIC_EXEMPT_WORDS:
        # Jack 2026-10-09: "turn off toxic halts on MENTION markets"
        lines.append(f"  Exempt: series containing "
                     f"{' / '.join(imm.TOXIC_EXEMPT_WORDS)} -- their pick-offs "
                     f"are listed below but never halt anything.")
    return lines


def _exempt_tag(ps) -> str:
    """' (exempt)' when the pick-offs are on an exempt series."""
    return " (exempt)" if any(p.get("exempt") for p in ps) else ""


def _c(v) -> str:
    """A price in cents, half cents kept: 51 -> '51', 50.5 -> '50.5'."""
    return f"{float(v):.1f}".rstrip("0").rstrip(".")


def _move_txt(r) -> str:
    if r["move"] is None:
        return "n/a"
    if abs(r["move"]) < 0.05:
        return f"{_c(r['m0'])}->{_c(r['m1'])} (flat)"
    word = "kept moving against" if r["move"] > 0 else "moved back"
    return f"{_c(r['m0'])}->{_c(r['m1'])} ({word} {_c(abs(r['move']))}c)"


def render_text(ctx) -> str:
    L = [f"IMM toxic-flow halts -- {ctx['day']:%a %Y-%m-%d} (ET day)", ""]
    L += _rule_lines() + [""]
    if not ctx["seen_file"]:
        L += ["No toxic_halts sink file for this day -- the rule was off, or "
              "the bot wrote nothing (check IMM_TOXIC_HALT / the analytics "
              "sinks).", ""]
    L.append(f"Totals: {ctx['n_picks']} pick-off(s) on {ctx['n_pick_mkts']} "
             f"market(s); {len(ctx['event_rows'])} event halt(s) on "
             f"{ctx['n_events']} event(s); {len(ctx['side_rows'])} side halt(s) "
             f"on {ctx['n_side_mkts']} market(s); est reward forgone "
             f"${ctx['forgone']:.2f}.")
    if not ctx["side_rows"] and not ctx["event_rows"]:
        L += ["", "Nothing was halted."]
    if ctx["event_rows"]:
        L += ["", "EVENT HALTS (whole event, both sides)",
              f"  {'ET':5s}  {'event':34s} {'mkts':>4s} {'forgone':>8s}  picked off"]
        for r in ctx["event_rows"][:MAX_ROWS]:
            L.append(f"  {_et(r['ts']):5s}  {r['event'][:34]:34s} {r['halted']:4d} "
                     f"{'$%.2f' % r['forgone']:>8s}  {', '.join(r['picked'])}")
    if ctx["side_rows"]:
        L += ["", "SIDE HALTS",
              f"  {'ET':5s}  {'market':40s} {'side':4s} {'forgone':>8s}  "
              f"pick-offs (fill->mark)  price during halt"]
        for r in ctx["side_rows"][:MAX_ROWS]:
            pk = ", ".join(f"{_c(a)}->{_c(b)}" for a, b in r["picks"]) or "n/a"
            L.append(f"  {_et(r['ts']):5s}  {r['ticker'][:40]:40s} "
                     f"{r['side'].upper():4s} {'$%.2f' % r['forgone']:>8s}  "
                     f"{pk}  {_move_txt(r)}")
    if ctx["top"]:
        L += ["", "MOST PICKED-OFF (including ones that never halted)"]
        for t, s, ps in ctx["top"]:
            worst = max(ps, key=lambda p: abs(p["mark"] - p["fill_px"]))
            L.append(f"  {len(ps):3d}x  {t[:44]:44s} {s.upper():4s} worst "
                     f"{_c(worst['fill_px'])}->{_c(worst['mark'])}{_exempt_tag(ps)}")
    L += ["", "Knobs: IMM_TOXIC_HALT=0 kills both rules; IMM_TOXIC_EVENT_MARKETS=0 "
          "the event rule alone; IMM_TOXIC_PICKOFF_CENTS / _PICKOFFS / "
          "_HALT_MIN / _EVENT_WINDOW_MIN / _EVENT_HALT_MIN tune them; "
          "IMM_TOXIC_EXEMPT lists the exempt series words."]
    return "\n".join(L)


def render_html(ctx) -> str:
    def table(head, rows):
        h = "".join(f'<th style="{TDL}background:#f3f3f3">{_esc(x)}</th>' for x in head)
        body = "".join("<tr>" + "".join(
            f'<td style="{TDL if i in (1, len(r) - 1) else TD}">{_esc(str(c))}</td>'
            for i, c in enumerate(r)) + "</tr>" for r in rows)
        return f'<table style="border-collapse:collapse;font:13px sans-serif">' \
               f'<tr>{h}</tr>{body}</table>'
    parts = [f"<h3 style='font:bold 15px sans-serif'>IMM toxic-flow halts -- "
             f"{ctx['day']:%a %Y-%m-%d} (ET)</h3>",
             "<p style='font:13px sans-serif'>" + "<br>".join(
                 _esc(x) for x in _rule_lines()) + "</p>"]
    if not ctx["seen_file"]:
        parts.append("<p style='font:13px sans-serif;color:#a00'>No toxic_halts "
                     "sink file for this day -- the rule was off, or nothing was "
                     "written.</p>")
    parts.append(
        f"<p style='font:13px sans-serif'><b>{ctx['n_picks']}</b> pick-off(s) on "
        f"{ctx['n_pick_mkts']} market(s); <b>{len(ctx['event_rows'])}</b> event "
        f"halt(s); <b>{len(ctx['side_rows'])}</b> side halt(s); est reward "
        f"forgone <b>${ctx['forgone']:.2f}</b>.</p>")
    if ctx["event_rows"]:
        parts.append("<h4 style='font:bold 13px sans-serif'>Event halts (whole "
                     "event, both sides)</h4>")
        parts.append(table(["ET", "event", "markets", "forgone", "picked off"], [
            (_et(r["ts"]), r["event"], r["halted"], f"${r['forgone']:.2f}",
             ", ".join(r["picked"])) for r in ctx["event_rows"][:MAX_ROWS]]))
    if ctx["side_rows"]:
        parts.append("<h4 style='font:bold 13px sans-serif'>Side halts</h4>")
        parts.append(table(["ET", "market", "side", "forgone", "pick-offs (fill->mark)",
                            "price during halt"], [
            (_et(r["ts"]), r["ticker"], r["side"].upper(), f"${r['forgone']:.2f}",
             ", ".join(f"{_c(a)}->{_c(b)}" for a, b in r["picks"]) or "n/a",
             _move_txt(r)) for r in ctx["side_rows"][:MAX_ROWS]]))
    if not ctx["side_rows"] and not ctx["event_rows"]:
        parts.append("<p style='font:13px sans-serif'>Nothing was halted.</p>")
    if ctx["top"]:
        parts.append("<h4 style='font:bold 13px sans-serif'>Most picked-off "
                     "(including ones that never halted)</h4>")
        rows = []
        for t, s, ps in ctx["top"]:
            worst = max(ps, key=lambda p: abs(p["mark"] - p["fill_px"]))
            rows.append((len(ps), t + _exempt_tag(ps), s.upper(),
                         f"{_c(worst['fill_px'])}->{_c(worst['mark'])}"))
        parts.append(table(["count", "market", "side", "worst"], rows))
    return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker")
    ap.add_argument("--day", help="ET day to report (YYYY-MM-DD); default yesterday")
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else today_et - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_toxic_halts_sent_{today_et}.marker")
    if not (args.test or args.dry or args.day) and os.path.exists(marker):
        log(f"toxic-halts email already sent for {today_et}; exiting")
        return 0
    ctx = build_report(day)
    text, html, subject = render_text(ctx), render_html(ctx), subject_for(ctx)
    log("toxic-halts body:\n" + text)
    if args.dry:
        return 0
    attempts = 1 if args.test else 8
    alerter = imm.Alerter("IMM-TOXIC", live=True)
    if not alerter.enabled:
        log("cannot send toxic-halts email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject, html=html)
        if ok:
            break
        log(f"toxic-halts send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"toxic-halts send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.day):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR,
                                          "imm_toxic_halts_sent_*.marker")):
            name = os.path.basename(old)[len("imm_toxic_halts_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
