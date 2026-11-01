#!/usr/bin/env python3
r"""send_rt_settled_monitor.py -- the daily email on RT SETTLED STRIKES (Jack
2026-11-01: "run it live, and then email report on performnce so i can
monitor it").

The rule (incentive_mm, RT SETTLED STRIKES) quotes a Rotten Tomatoes strike
far from a well-reviewed film's score -- 50+ reviews, 5+ points away, the RT
model at 97%+ on that side and the book agreeing -- up to 98c and through the
release week. While the rule is what quotes a strike (past the release-week
stand-down, or a touch outside the family's normal band) the bot logs
"rt settled <t>: on -- ...", and "off -- <why>" when that ends; a restart
banner ends every span, and the new process logs its own.

THE EMAIL, for the prior ET day, money first:
  net       the reward the bot estimated on those strikes while the rule
            quoted them (its hourly cache, pro rata) plus the day's fills on
            them, valued at the latest mark or the settlement; and every day
            since the rule went live (as each report found it)
  open      our open contracts on every strike the rule has quoted, their
            value now, and what they would lose if each film's score swung
            past its strike
  strikes   one line per strike: hours quoted, reward, fills, value now
  settled   strikes we held that settled: did the side the model called win?
Verdict: STOP when a strike settled against the side the model called, or
the net since go-live is STOP_NET_USD or worse; WATCH on a losing day or
more than WATCH_OPEN_USD open against a swing; else OK.

SOURCES, read-only: the bot's day logs (incentive-mm-*.log), fills_ /
settlements_<UTC date>.jsonl, the hourly reward cache, the marks sink, the
IMM dashboard (positions), rt_fair.json (titles). It writes only its
sent-marker and rt_settled_report.json (each day's totals and the strikes
the rule has quoted). Imports send_comp_lead_monitor (and so imm_quote_gaps
first, like the other morning emails) for its readers.

USAGE:
  python send_rt_settled_monitor.py                   # the scheduled daily run
  python send_rt_settled_monitor.py --dry             # build + print, send nothing
  python send_rt_settled_monitor.py --test            # send now, ignore the marker
  python send_rt_settled_monitor.py --day 2026-11-01  # a specific ET day
Scheduled daily as "KL imm rt-settled" (register_imm_rt_settled.ps1), 08:05
ET. Sends every morning, so a silent day is distinguishable from a broken
task. All console output is ASCII (the task console is cp1252).
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from html import escape as _esc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_comp_lead_monitor as lm  # noqa: E402  (imports imm_quote_gaps first)

imm = lm.imm
log = lm.log

STORE_FILE = "rt_settled_report.json"   # in STATUS_DIR: each day's totals, strikes seen
SPAN_LOOKBACK_SECS = 3 * 86400          # day logs read this far back: a span opened before
SPAN_FILL_SLACK_SECS = 60               # a fill this close to a span counts in it
STOP_NET_USD = -50.0                    # the net since go-live at which to stop
WATCH_OPEN_USD = 300.0                  # open contracts that could lose this on a swing
LEVELS = ("OK", "WATCH", "STOP")

_ON = re.compile(r"^(\S+ \S+)Z \[IMM\] rt settled (\S+): on -- (\d+) reviews, score "
                 r"([\d.]+)%, strike ([\d.]+), model ([\d.]+)%, book mid ([\d.]+)c")
_OFF = re.compile(r"^(\S+ \S+)Z \[IMM\] rt settled (\S+): off -- (.*)$")


def log_lines(a_ts: float):
    """The day logs' rt-settled lines and restart banners, oldest first, from
    the files that can hold anything since SPAN_LOOKBACK_SECS before a_ts."""
    for fn in sorted(glob.glob(os.path.join(imm.STATUS_DIR, "incentive-mm-*.log"))):
        try:
            if os.path.getmtime(fn) < a_ts - SPAN_LOOKBACK_SECS:
                continue
            fh = open(fn, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if "rt settled " in line or "=== incentive_mm" in line:
                    yield line.rstrip("\n")


def parse_spans(lines, a_ts: float, b_ts: float) -> dict:
    """{ticker: {"spans": [[start, end], ...], "info": {n, score, strike,
    model, mid} of its last on line, "on_at_end"}} inside [a_ts, b_ts): a
    span opens on an on line and ends on an off line or a restart banner."""
    open_: dict = {}
    spans: dict = defaultdict(list)
    info: dict = {}
    for line in lines:
        m = lm._BANNER.match(line)
        if m:
            ts = lm._ts(m.group(1))
            for t, s in open_.items():
                spans[t].append([s, ts])
            open_.clear()
            continue
        m = _ON.match(line)
        if m:
            t = m.group(2)
            open_.setdefault(t, lm._ts(m.group(1)))
            info[t] = dict(n=int(m.group(3)), score=float(m.group(4)),
                           strike=float(m.group(5)), model=float(m.group(6)),
                           mid=float(m.group(7)))
            continue
        m = _OFF.match(line)
        if m:
            s = open_.pop(m.group(2), None)
            if s is not None:
                spans[m.group(2)].append([s, lm._ts(m.group(1))])
    for t, s in open_.items():
        spans[t].append([s, b_ts])
    out = {}
    for t, sp in spans.items():
        cl = [[max(s, a_ts), min(e, b_ts)] for s, e in sp if e > a_ts and s < b_ts]
        cl = [x for x in cl if x[1] > x[0]]
        if cl:
            out[t] = {"spans": lm._union(cl), "info": info.get(t, {}),
                      "on_at_end": t in open_}
    return out


def rewards(spans: dict, hourly: dict) -> dict:
    """{ticker: the bot's estimated reward while the rule quoted it}: its
    hourly estimate over its spans, pro rata within an hour."""
    out = {}
    for t, v in spans.items():
        hs = hourly.get(t) or {}
        tot = 0.0
        for s, e in v["spans"]:
            h = int(s // 3600)
            while h * 3600 < e:
                ov = min(e, (h + 1) * 3600.0) - max(s, h * 3600.0)
                if ov > 0:
                    tot += hs.get(h, 0.0) * ov / 3600.0
                h += 1
        out[t] = tot
    return out


def span_fills(fills, spans: dict) -> list:
    """Our maker fills on a strike while the rule quoted it."""
    out = []
    for f in fills:
        t = f.get("ticker")
        if t not in spans or f.get("is_taker"):
            continue
        ts = lm.row_ts(f)
        if not any(s - SPAN_FILL_SLACK_SECS <= ts <= e + SPAN_FILL_SLACK_SECS
                   for s, e in spans[t]["spans"]):
            continue
        px = f.get("yes_price_cents")
        if px is None:
            continue
        side = f.get("our_book_side") or (
            "bid" if (f.get("side") == "yes") == (f.get("action") == "buy") else "ask")
        out.append(dict(ticker=t, side=side, ts=ts, px=float(px),
                        count=float(f.get("count") or 0)))
    return out


def value(fills: list, marks: dict) -> None:
    """Each fill's `mark` and `usd` at it (+ = made money); None unmarked."""
    for f in fills:
        m = marks.get(f["ticker"])
        f["mark"] = m
        f["usd"] = (None if m is None else
                    (m - f["px"]) * f["count"] / 100.0 * (1 if f["side"] == "bid" else -1))


def swing_loss(pos: float, avg: float, info: dict) -> float:
    """What an open position loses if the film's score swings past the
    strike, against the side the model called: a long YES on a strike under
    the score loses its cost, a short YES on one over the score loses the
    rest of the dollar; the other way round a swing only helps."""
    if not info or not pos:
        return 0.0
    yes_called = info.get("score", 0.0) > info.get("strike", 0.0)
    if yes_called and pos > 0:
        return pos * avg / 100.0
    if not yes_called and pos < 0:
        return -pos * (100.0 - avg) / 100.0
    return 0.0


def load_store() -> dict:
    try:
        with open(os.path.join(imm.STATUS_DIR, STORE_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_store(data: dict) -> None:
    path = os.path.join(imm.STATUS_DIR, STORE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def load_dashboard() -> dict:
    path = os.path.join(imm.STATUS_DIR, "dashboard", "imm_dashboard.html")
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
        i = html.index("const D = ") + len("const D = ")
        return json.JSONDecoder().raw_decode(html, i)[0]
    except (OSError, ValueError):
        return {}


def titles() -> dict:
    """{event: film title} off rt_fair.json's open events."""
    try:
        with open(imm.RT_FAIR_FILE, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return {}
    return {ev: str(e.get("title") or "") for ev, e in (data.get("open_events") or {}).items()}


def verdict(ctx: dict) -> tuple:
    """(level, reasons)."""
    level, why = 0, []
    against = [s for s in ctx["settled"] if not s["as_called"]]
    if against:
        level = 2
        why.append(f"{len(against)} strike(s) settled against the side the model called")
    if ctx["since"]["net"] <= STOP_NET_USD:
        level = 2
        why.append(f"net since go-live {usd(ctx['since']['net'])}, at or under "
                   f"{usd(STOP_NET_USD)}")
    if ctx["net"] < 0:
        level = max(level, 1)
        why.append(f"the day lost {usd(ctx['net'])}")
    if ctx["open"]["at_risk"] > WATCH_OPEN_USD:
        level = max(level, 1)
        why.append(f"{usd(ctx['open']['at_risk'])} open against a swing, over "
                   f"{usd(WATCH_OPEN_USD)}")
    return LEVELS[level], why


def build_report(day, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    a_ts, b_ts = lm.day_window(day)
    b_ts = min(b_ts, now.timestamp())
    spans = parse_spans(log_lines(a_ts), a_ts, b_ts)
    hourly, _cov = lm.load_hourly(a_ts, b_ts)
    rew = rewards(spans, hourly)
    fills = span_fills(lm.jsonl_rows("fills", a_ts, b_ts), spans)
    marks = lm.load_marks(a_ts, now.timestamp())
    value(fills, marks)
    store = load_store()
    seen = dict(store.get("strikes") or {})
    for t, v in spans.items():
        seen[t] = v["info"] or seen.get(t) or {}
    # the strikes the rule has quoted that we held and that settled in the
    # window (the settlements sink holds our own), and those settled since
    results_day = lm.load_results(a_ts, b_ts)
    results_now = lm.load_results(a_ts, now.timestamp())
    D = load_dashboard()
    mk = D.get("markets") or {}
    settled = []
    for t, inf in sorted(seen.items()):
        if t in results_day and inf:
            won = results_day[t] >= 50
            settled.append(dict(ticker=t, result="YES" if won else "NO",
                                as_called=won == (inf.get("score", 0.0) > inf.get("strike", 0.0))))
    open_rows = []
    for t, inf in sorted(seen.items()):
        m = mk.get(t) or {}
        pos = float(m.get("pos") or 0.0)
        if not pos or t in results_now:
            continue
        avg = float(m.get("avg") or 0.0)
        mark = marks.get(t)
        open_rows.append(dict(ticker=t, pos=pos, avg=avg, mark=mark,
                              value=(None if mark is None else (mark - avg) * pos / 100.0),
                              at_risk=swing_loss(pos, avg, inf)))
    net_rew = sum(rew.values())
    net_fills = sum(f["usd"] or 0.0 for f in fills)
    day_key = day.isoformat()
    days = dict(store.get("days") or {})
    days[day_key] = dict(rewards=round(net_rew, 2), fills=round(net_fills, 2),
                         net=round(net_rew + net_fills, 2), strikes=len(spans))
    since = dict(net=sum(v["net"] for v in days.values()), days=len(days),
                 first=min(days) if days else day_key)
    ctx = dict(day=day_key, now=now.isoformat(), spans=spans, rewards=rew, fills=fills,
               net_rewards=net_rew, net_fills=net_fills, net=net_rew + net_fills,
               hours=sum(e - s for v in spans.values() for s, e in v["spans"]) / 3600.0,
               settled=settled, titles=titles(), since=since,
               open=dict(rows=open_rows, contracts=sum(abs(r["pos"]) for r in open_rows),
                         value=sum(r["value"] or 0.0 for r in open_rows),
                         at_risk=sum(r["at_risk"] for r in open_rows)),
               store=dict(store, days=days, strikes=seen))
    ctx["level"], ctx["reasons"] = verdict(ctx)
    return ctx


def usd(x: float) -> str:
    return ("-" if x < 0 else "+") + f"${abs(x):,.2f}"


def _name(ctx, t: str) -> str:
    ev, k = t.rsplit("-", 1)
    title = ctx["titles"].get(ev) or ev
    return f"{title} {k}"


def render_text(ctx) -> str:
    day = datetime.strptime(ctx["day"], "%Y-%m-%d")
    fills = ctx["fills"]
    cts = sum(f["count"] for f in fills)
    out = [f"RT settled strikes, {day:%a %m/%d} (ET day)",
           "The rule quotes Rotten Tomatoes strikes far from a well-reviewed film's score "
           f"({imm.RT_SETTLED_MIN_REVIEWS}+ reviews, {imm.RT_SETTLED_MIN_GAP:g}+ points away, "
           f"the RT model at {imm.RT_SETTLED_P * 100:g}%+), up to {imm.RT_SETTLED_BAND_HI}c "
           "and into the release week.",
           "",
           f"NET {usd(ctx['net'])}: rewards {usd(ctx['net_rewards'])} on {len(ctx['spans'])} "
           f"strike(s) over {ctx['hours']:.1f} market-hours, and {len(fills)} fill(s) "
           f"({cts:,.0f} contracts) worth {usd(ctx['net_fills'])} at today's prices.",
           f"Since it went live ({ctx['since']['first']}): net {usd(ctx['since']['net'])} "
           f"over {ctx['since']['days']} day(s), each day as its report found it.",
           ""]
    o = ctx["open"]
    if o["rows"]:
        out.append(f"OPEN NOW: {o['contracts']:,.0f} contracts on {len(o['rows'])} strike(s), "
                   f"worth {usd(o['value'])} at today's prices; if every film's score swung "
                   f"past its strike they would lose {usd(-o['at_risk'])}.")
    else:
        out.append("OPEN NOW: nothing held on these strikes.")
    lvl = ctx["level"]
    out += ["", f"VERDICT: {lvl}" + (" -- " + "; ".join(ctx["reasons"]) if ctx["reasons"]
                                     else " -- nothing to act on")]
    if ctx["spans"]:
        out += ["", "STRIKES"]
        for t, v in sorted(ctx["spans"].items(), key=lambda kv: -ctx["rewards"].get(kv[0], 0)):
            inf = v["info"]
            fl = [f for f in fills if f["ticker"] == t]
            hrs = sum(e - s for s, e in v["spans"]) / 3600.0
            out.append(f"  {_name(ctx, t)}"
                       + (f" (score {inf['score']:g}, {inf['n']} reviews)" if inf else "")
                       + f": {hrs:.1f}h quoted, reward {usd(ctx['rewards'].get(t, 0.0))}, "
                       + (f"{len(fl)} fill(s) ({sum(f['count'] for f in fl):,.0f} ct) "
                          f"{usd(sum(f['usd'] or 0.0 for f in fl))}" if fl else "no fills"))
    if ctx["settled"]:
        out += ["", "SETTLED"]
        for s in ctx["settled"]:
            out.append(f"  {_name(ctx, s['ticker'])} settled {s['result']}, "
                       + ("as the model called" if s["as_called"] else "AGAINST the model"))
    if o["rows"]:
        out += ["", "HELD"]
        for r in sorted(o["rows"], key=lambda r: -r["at_risk"]):
            out.append(f"  {_name(ctx, r['ticker'])}: {r['pos']:+,.0f} @ {r['avg']:.0f}c, "
                       + ("no mark" if r["mark"] is None else f"now {r['mark']:.1f}c")
                       + f", {usd(-r['at_risk'])} on a swing")
    return "\n".join(out)


def render_html(ctx) -> str:
    lines = render_text(ctx).split("\n")
    body = []
    for ln in lines:
        if not ln:
            continue
        if ln.startswith("  "):
            body.append(f"<li>{_esc(ln.strip())}</li>")
        elif ln in ("STRIKES", "SETTLED", "HELD"):
            body.append(f"<h4 style='margin:12px 0 4px'>{_esc(ln)}</h4>")
        else:
            body.append(f"<p style='margin:6px 0'>{_esc(ln)}</p>")
    html, in_list = [], False
    for b in body:
        if b.startswith("<li>") and not in_list:
            html.append("<ul style='margin:4px 0;padding-left:18px'>")
            in_list = True
        elif not b.startswith("<li>") and in_list:
            html.append("</ul>")
            in_list = False
        html.append(b)
    if in_list:
        html.append("</ul>")
    return ("<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.4'>"
            + "".join(html) + "</div>")


def subject_for(ctx) -> str:
    day = datetime.strptime(ctx["day"], "%Y-%m-%d")
    return (f"RT settled strikes {day:%m/%d}: net {usd(ctx['net'])} (rewards "
            f"{usd(ctx['net_rewards'])}, {len(ctx['fills'])} fills {usd(ctx['net_fills'])}) "
            f"| {ctx['level']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker or store written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker, no store")
    ap.add_argument("--day", help="ET day to report (YYYY-MM-DD); default yesterday")
    args = ap.parse_args(argv)
    lm.low_priority()
    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else today_et - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_rt_settled_sent_{today_et}.marker")
    if not (args.test or args.dry or args.day) and os.path.exists(marker):
        log(f"rt settled email already sent for {today_et}; exiting")
        return 0
    ctx = build_report(day, now=now_utc)
    text, html, subject = render_text(ctx), render_html(ctx), subject_for(ctx)
    log("rt settled email: " + subject + "\n" + text)
    if args.dry:
        return 0
    alerter = imm.Alerter("IMM-RT", live=True)
    if not alerter.enabled:
        log("cannot send rt settled email: alert credentials not configured")
        return 1
    attempts = 1 if args.test else 8
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=("[TEST] " if args.test else "") + subject,
                                  html=html)
        if ok:
            break
        log(f"rt settled send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"rt settled send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not args.test:
        save_store(ctx["store"])
    if not (args.test or args.day):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR, "imm_rt_settled_sent_*.marker")):
            name = os.path.basename(old)[len("imm_rt_settled_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
