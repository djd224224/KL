#!/usr/bin/env python3
r"""send_comp_lead_monitor.py -- the daily complement-lead and hedge email
(Jack 2026-10-29: "I mean just a daily status email report on this
mechanism").

THE MECHANISM (incentive_mm COMP_LEAD_MODE / COMP_HEDGE_MODE, a042520): in an
event Kalshi marks mutually exclusive, a side whose own book sits far behind
the price its sibling markets imply LEADS with one rung of target/5 (200) at
the cheapest price that takes ~95% of that side's reward score -- the sell
side in any family, the buy side in two-market election pairs. A lead that
fills is hedged with immediate-or-cancel limit orders on every sibling while
the whole set still locks COMP_HEDGE_EDGE_C a set; an order resting ahead of a
lead ends it and the side joins that order.

ONE EMAIL per ET day (the day before), ending in OK / WATCH / PROBLEM:
  leads    every side that led, at what price against its own best and the
           price its siblings imply, for how long; the sides leading at the
           day's end
  fills    our maker fills at a lead's price: count, contracts, dollars
  hedges   jobs opened, legs sent and filled, sets locked and their edge,
           jobs given up, sends Kalshi refused; jobs still open now
  estimate the bot's own reward estimate now for the markets that led
           (its hourly selection snapshot; Kalshi pays ~1.15x it on average)
  verdict  PROBLEM: a job gave up, a send was refused, or a job has had
           unhedged legs for over HEDGE_STUCK_SECS; WATCH: a lead fill
           opened no job, or a job is open; OK otherwise

SOURCES, read-only: the bot's day logs (incentive-mm-*.log), fills_ /
comp_hedge_ / selection_snapshot_<UTC date>.jsonl and comp_hedge_jobs.json.
No Kalshi reads, no orders. Imports imm_quote_gaps first, like the other
morning emails, so the launcher env and the alert credentials apply.

USAGE:
  python send_comp_lead_monitor.py                   # the scheduled daily run
  python send_comp_lead_monitor.py --dry             # build + print, send nothing
  python send_comp_lead_monitor.py --test            # send now, ignore the marker
  python send_comp_lead_monitor.py --day 2026-10-29  # a specific ET day
Scheduled daily as "KL imm comp lead" (register_imm_comp_lead.ps1), 07:55 ET.
Sends every morning, so a silent day is distinguishable from a broken task.
All console output is ASCII (the task console is cp1252).
"""
import argparse
import ctypes
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_quote_gaps as _gaps  # noqa: F401,E402  (import side effects)
import incentive_mm as imm  # noqa: E402
from incentive_mm import log  # noqa: E402

LOOKBACK_SECS = 6 * 3600        # day-log read starts this far before the window
HEDGE_STUCK_SECS = 3600         # an open job with unhedged legs this old is a PROBLEM
LEVELS = ("OK", "WATCH", "PROBLEM")

_LEAD = re.compile(
    r"^(\S+ \S+)Z \[IMM\] comp lead( \(dry\))? (\S+) (bid|ask): (\d+) @ (\d+)c, "
    r"(?:ahead of|held at the cap, (?:with|behind)) its own (\d+)c; (.+?) "
    r"(?:implies|imply) (-?\d+)c")
_OFF = re.compile(r"^(\S+ \S+)Z \[IMM\] comp lead(?: \(dry\))? (\S+) (bid|ask): off -- ")
_GROUPS = re.compile(r"^(\S+ \S+)Z \[IMM\] comp lead: (\d+) mutually exclusive "
                     r"event\(s\) grouped, (\d+) of them two-market")
_BANNER = re.compile(r"^(\S+ \S+)Z === incentive_mm")


def low_priority() -> None:
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40)      # IDLE_PRIORITY_CLASS
    except Exception:
        pass


def _ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def day_window(day) -> tuple:
    """[00:00 ET, next 00:00 ET) of an ET date, as UTC epoch seconds."""
    a = imm.ET.localize(datetime(day.year, day.month, day.day))
    b = imm.ET.localize(datetime(day.year, day.month, day.day) + timedelta(days=1))
    return a.timestamp(), b.timestamp()


def utc_dates(a_ts: float, b_ts: float) -> list:
    d = datetime.fromtimestamp(a_ts, timezone.utc).date()
    last = datetime.fromtimestamp(b_ts - 1, timezone.utc).date()
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def day_log_lines(a_ts: float):
    """Lines of every day log that can hold the window, oldest file first."""
    for fn in sorted(glob.glob(os.path.join(imm.STATUS_DIR, "incentive-mm-*.log"))):
        try:
            if os.path.getmtime(fn) < a_ts - LOOKBACK_SECS:
                continue
        except OSError:
            continue
        with open(fn, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line


def jsonl_rows(prefix: str, a_ts: float, b_ts: float):
    """Rows of STATUS_DIR/<prefix>_<UTC date>.jsonl covering the window."""
    for d in utc_dates(a_ts, b_ts):
        try:
            with open(os.path.join(imm.STATUS_DIR, f"{prefix}_{d}.jsonl"),
                      encoding="utf-8") as fh:
                for line in fh:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            continue


def row_ts(r: dict) -> float:
    v = r.get("ts")
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def parse_leads(lines, a_ts: float, b_ts: float) -> dict:
    """{(ticker, side): {"spans": [[start, end], ...], "px", "best", "implied",
    "who", "dry", "open"}} for the window from the day-log lines. A restart
    banner closes every open lead (a new process logs its leads afresh)."""
    leads: dict = {}
    groups = None
    for line in lines:
        m = _LEAD.match(line)
        if m:
            ts = _ts(m.group(1))
            if ts >= b_ts:
                continue
            key = (m.group(3), m.group(4))
            ld = leads.setdefault(key, {"spans": [], "open": None, "pxs": []})
            if ld["open"] is None:
                ld["open"] = ts
            if a_ts <= ts:
                ld["pxs"].append(int(m.group(6)))
            ld.update(px=int(m.group(6)), best=int(m.group(7)), who=m.group(8),
                      implied=int(m.group(9)), dry=bool(m.group(2)), size=int(m.group(5)))
            continue
        m = _OFF.match(line) or _BANNER.match(line)
        if m:
            ts = _ts(m.group(1))
            if ts >= b_ts:
                continue
            keys = ([(m.group(2), m.group(3))] if m.re is _OFF
                    else [k for k, v in leads.items() if v["open"] is not None])
            for key in keys:
                ld = leads.get(key)
                if ld and ld["open"] is not None:
                    ld["spans"].append([ld["open"], ts])
                    ld["open"] = None
            continue
        m = _GROUPS.match(line)
        if m and _ts(m.group(1)) < b_ts:
            groups = (int(m.group(2)), int(m.group(3)))
    out = {}
    for key, ld in leads.items():
        spans = [[max(s, a_ts), min(e, b_ts)] for s, e in ld["spans"] + (
            [[ld["open"], b_ts]] if ld["open"] is not None else []) if e > a_ts]
        spans = [sp for sp in spans if sp[1] > sp[0]]
        if spans:
            out[key] = dict(ld, spans=spans, leading_at_end=ld["open"] is not None)
    return {"leads": out, "groups": groups}


def lead_fills(fills: list, leads: dict) -> list:
    """Our maker fills on a side while it led, at the price it led at."""
    out = []
    for f in fills:
        if f.get("is_taker"):
            continue
        side = f.get("our_book_side") or (
            "bid" if (f.get("side") == "yes") == (f.get("action") == "buy") else "ask")
        ld = leads.get((f.get("ticker"), side))
        if not ld:
            continue
        ts = row_ts(f)
        px = f.get("yes_price_cents")
        if any(s - 60 <= ts <= e + 600 for s, e in ld["spans"]) and px is not None \
                and abs(float(px) - ld["px"]) <= 0.5:
            out.append(dict(ticker=f.get("ticker"), side=side, ts=ts,
                            count=float(f.get("count") or 0), px=float(px)))
    return out


def hedge_summary(rows: list, jobs: list, now_ts: float) -> dict:
    """The day's hedge rows (comp_hedge_<date>.jsonl) and the open jobs now."""
    opened = [r for r in rows if r.get("kind") == "open"]
    done = [r for r in rows if r.get("kind") == "done"]
    expired = [r for r in rows if r.get("kind") == "expired"]
    sends = [r for r in rows if r.get("kind") == "send" and not r.get("dry")]
    refused = [r for r in sends if r.get("error")]
    leg_fills = [r for r in rows if r.get("kind") == "leg_fill"]
    locked = sum(float(r.get("edge_c") or 0.0) * float(r.get("count") or 0.0) / 100.0
                 for r in done)
    open_now = []
    for j in jobs:
        q = float(j.get("count") or 0.0)
        rem = {s: max(0.0, q - float(lg.get("filled", 0.0)))
               for s, lg in (j.get("legs") or {}).items()}
        open_now.append(dict(id=j.get("id"), ticker=j.get("ticker"), side=j.get("side"),
                             count=q, price=j.get("price"),
                             age_s=now_ts - float(j.get("at") or now_ts),
                             unhedged={s: r for s, r in rem.items() if r >= 0.5},
                             waiting=bool(j.get("waiting"))))
    return dict(opened=opened, done=done, expired=expired, sends=sends,
                refused=refused, leg_fills=leg_fills, locked=locked, open_now=open_now)


def latest_estimates(tickers: set, a_ts: float, b_ts: float) -> dict:
    """ticker -> (est $/day, est share) from the latest selection snapshot rows."""
    out = {}
    for r in jsonl_rows("selection_snapshot", a_ts, b_ts + 86400):
        t = r.get("ticker")
        if t in tickers:
            out[t] = (float(r.get("est_dollars_per_day") or 0.0),
                      float(r.get("est_frac") or 0.0))
    return out


def load_jobs() -> list:
    try:
        with open(os.path.join(imm.STATUS_DIR, imm.COMP_HEDGE_FILE), encoding="utf-8") as f:
            return [j for j in (json.load(f).get("jobs") or []) if isinstance(j, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def verdict(ctx: dict) -> tuple:
    reasons, level = [], 0

    def flag(lv, why):
        nonlocal level
        level = max(level, lv)
        reasons.append(why)

    h = ctx["hedges"]
    if h["expired"]:
        flag(2, f"{len(h['expired'])} hedge job(s) gave up with legs unhedged")
    if h["refused"]:
        flag(2, f"Kalshi refused {len(h['refused'])} hedge order(s)")
    stuck = [j for j in h["open_now"] if j["unhedged"] and j["age_s"] >= HEDGE_STUCK_SECS]
    if stuck:
        flag(2, f"{len(stuck)} hedge job(s) unhedged for over "
                f"{HEDGE_STUCK_SECS // 60} min")
    if len(ctx["fills"]) > len(h["opened"]):
        flag(1, f"{len(ctx['fills'])} lead fill(s) but {len(h['opened'])} hedge job(s) "
                f"opened")
    young = [j for j in h["open_now"] if j not in stuck]
    if young:
        flag(1, f"{len(young)} hedge job(s) still working")
    return LEVELS[level], reasons


def usd(x: float) -> str:
    return ("-$" if x < 0 else "+$") + f"{abs(x):,.2f}"


def build_report(day, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    a_ts, b_ts = day_window(day)
    parsed = parse_leads(day_log_lines(a_ts), a_ts, b_ts)
    leads = parsed["leads"]
    fills = lead_fills(list(jsonl_rows("fills", a_ts, b_ts)), leads)
    rows = [r for r in jsonl_rows("comp_hedge", a_ts, b_ts) if a_ts <= row_ts(r) < b_ts]
    hedges = hedge_summary(rows, load_jobs(), now.timestamp())
    est = latest_estimates({t for t, _s in leads}, a_ts, b_ts)
    ctx = dict(day=day.isoformat(), leads=leads, groups=parsed["groups"], fills=fills,
               hedges=hedges, est=est, now=now.isoformat())
    ctx["level"], ctx["reasons"] = verdict(ctx)
    return ctx


def subject_for(ctx) -> str:
    day = datetime.strptime(ctx["day"], "%Y-%m-%d")
    n_led = len(ctx["leads"])
    h = ctx["hedges"]
    return (f"IMM complement lead {day:%m/%d}: {n_led} side{'s' if n_led != 1 else ''} led, "
            f"{len(ctx['fills'])} lead fill{'s' if len(ctx['fills']) != 1 else ''}, "
            f"{len(h['done'])} hedged ({usd(h['locked'])} locked) | {ctx['level']}")


def render_text(ctx) -> str:
    day = datetime.strptime(ctx["day"], "%Y-%m-%d")
    out = [f"Complement lead and hedge, {day:%a %m/%d} (ET day)", ""]
    g = ctx["groups"]
    if g:
        out.append(f"Groups: {g[0]} mutually exclusive events read, {g[1]} of them "
                   f"two-market.")
    leads = ctx["leads"]
    now_leading = [k for k, v in leads.items() if v["leading_at_end"]]
    out += ["", f"LEADS: {len(leads)} side(s) led during the day; "
                f"{len(now_leading)} leading at the day's end."]
    for (t, side), v in sorted(leads.items(), key=lambda kv: kv[0]):
        hrs = sum(e - s for s, e in v["spans"]) / 3600.0
        pxs = v.get("pxs") or []
        moved = (f", moved {min(pxs)}-{max(pxs)}c" if pxs and min(pxs) != max(pxs) else "")
        est = ctx["est"].get(t)
        est_s = f"; est now ${est[0]:.2f}/day ({100 * est[1]:.0f}% of pool)" if est else ""
        out.append(f"  {t} {side}: {v.get('size', 200)} @ {v['px']}c, own best "
                   f"{v['best']}c, siblings imply {v['implied']}c; led {hrs:.1f}h{moved}"
                   + (" (dry)" if v.get("dry") else "")
                   + ("" if v["leading_at_end"] else ", ended") + est_s)
    fills = ctx["fills"]
    out += ["", f"LEAD FILLS: {len(fills)}"]
    for f in fills:
        when = datetime.fromtimestamp(f["ts"], timezone.utc).astimezone(imm.ET)
        what = "sold" if f["side"] == "ask" else "bought"
        out.append(f"  {when:%H:%M} ET {f['ticker']} {what} {f['count']:g} @ {f['px']:g}c")
    h = ctx["hedges"]
    out += ["", f"HEDGES: {len(h['opened'])} job(s) opened, {len(h['sends'])} order(s) "
                f"sent, {len(h['leg_fills'])} leg fill(s), {len(h['done'])} set(s) locked "
                f"({usd(h['locked'])}), {len(h['expired'])} given up, "
                f"{len(h['refused'])} refused."]
    for r in h["done"]:
        out.append(f"  locked {r.get('job')}: {float(r.get('count') or 0):g} sets at "
                   f"{float(r.get('edge_c') or 0):+.1f}c a set")
    for r in h["refused"]:
        out.append(f"  refused {r.get('ticker')} {r.get('side')} {r.get('count')} @ "
                   f"{r.get('px')}c: {r.get('error_body') or r.get('error')}")
    for j in h["open_now"]:
        un = ", ".join(f"{s} {r:g}" for s, r in j["unhedged"].items()) or "none"
        out.append(f"  open {j['id']}: {j['age_s'] / 60:.0f} min old"
                   + (", waiting for the siblings' books" if j["waiting"] else "")
                   + f"; unhedged: {un}")
    tot = sum(v[0] for v in ctx["est"].values())
    out += ["", f"ESTIMATE: the markets that led are estimated at ${tot:.2f}/day now "
                f"(the bot's share model; Kalshi pays ~1.15x it on average)."]
    out += ["", f"VERDICT: {ctx['level']}"] + [f"  - {r}" for r in ctx["reasons"]]
    out += ["", "Switches (run_incentive_mm.ps1, then restart_imm.ps1 -Task): "
                "IMM_COMP_LEAD=dry|off stops placing leads; IMM_COMP_HEDGE=dry|off stops "
                "the hedge orders."]
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker")
    ap.add_argument("--day", help="ET day to report (YYYY-MM-DD); default yesterday")
    args = ap.parse_args(argv)
    low_priority()

    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else today_et - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_comp_lead_monitor_sent_{today_et}.marker")
    if not (args.test or args.dry or args.day) and os.path.exists(marker):
        log(f"complement lead email already sent for {today_et}; exiting")
        return 0
    ctx = build_report(day, now=now_utc)
    text, subject = render_text(ctx), subject_for(ctx)
    log("complement lead email: " + subject + "\n" + text)
    if args.dry:
        return 0
    attempts = 1 if args.test else 8
    alerter = imm.Alerter("IMM-COMPLEAD", live=True)
    if not alerter.enabled:
        log("cannot send complement lead email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject)
        if ok:
            break
        log(f"complement lead send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"complement lead send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.day):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR, "imm_comp_lead_monitor_sent_*.marker")):
            name = os.path.basename(old)[len("imm_comp_lead_monitor_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
