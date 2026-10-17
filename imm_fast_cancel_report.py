#!/usr/bin/env python3
"""The faster fast cancel's 3-day report (Jack 2026-10-17: "Yes, and email
report in 3 days").

Since 2026-10-17 the IMM cancels an ACTING stale rung (live, at least the
narrow gate -- IMM_WS_FAST_MIN_GAP_C, 10c -- ahead of the rest of its side)
0.25s after it first sees it there (arm "fast"). A random 20% of episodes,
drawn at that first sighting, keep the old 1s rule (arm "control"). This
scores the two arms from the bot's own logs and sizes the 1-9c gaps the gate
leaves alone:

  ws_stale_<UTC date>.jsonl   seen / unseen / flag / cancel lines (arm, seen_s)
  fills_<date>.jsonl          our maker fills (order_id, ts, count)
  dashboard/imm_dashboard.html  the `const D` object: each fill's 5-min and
                              4-hour mark-outs (cents a contract, + = ours)
  reward_est_hourly_cache.json  our reward per market-hour, to price the time a
                              cancelled rung spends off the book

Writes run-logs/incentive-mm/fast_cancel_report_<ET date>.md and prints its
path. Read-only: no Kalshi call, nothing in the bot touched.

  python imm_fast_cancel_report.py [--since ISO] [--until ISO] [--dir DIR]
"""
import argparse
import collections
import glob
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                                  # pragma: no cover
    ET = timezone(timedelta(hours=-4))

DEFAULT_DIR = r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm"
GRACE_S = 2.0          # a fill logged up to 2s after its episode ended still counts
OFF_BOOK_S = 11.0      # a cancelled rung waits ~half a cycle (~22s) to be re-placed
TERMINAL = {"unseen", "cancel", "clear", "amend", "gone"}


def _ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def load_stale(d, since, until):
    rows = []
    day = datetime.fromtimestamp(since, timezone.utc).date() - timedelta(days=1)
    last = datetime.fromtimestamp(until, timezone.utc).date() + timedelta(days=1)
    while day <= last:
        p = os.path.join(d, f"ws_stale_{day.isoformat()}.jsonl")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if since - 5 <= float(r.get("ts") or 0) <= until + 5:
                        rows.append(r)
        day += timedelta(days=1)
    rows.sort(key=lambda r: r["ts"])
    return rows


def episodes_from(rows):
    """One episode per `seen` line, ended by the order's next terminal line
    (unseen / cancel / clear / amend / gone)."""
    open_ep = {}
    eps = []
    for r in rows:
        ev, oid = r.get("ev"), r.get("order_id")
        if not oid:
            continue
        if ev == "seen":
            if oid in open_ep:                     # never closed: end it here
                open_ep[oid]["end"] = r["ts"]
                open_ep[oid]["how"] = "reseen"
            ep = {"oid": oid, "t": r.get("ticker"), "side": r.get("side"),
                  "px": r.get("px"), "start": r["ts"], "arm": r.get("arm"),
                  "acts": bool(r.get("acts")), "gap": r.get("gap_c"),
                  "end": None, "how": None, "fast_cancel": False}
            open_ep[oid] = ep
            eps.append(ep)
        elif ev in TERMINAL and oid in open_ep:
            ep = open_ep.pop(oid)
            ep["end"] = r["ts"]
            ep["how"] = ev if ev != "unseen" else f"unseen:{r.get('why')}"
            if ev == "cancel" and r.get("by") == "fast":
                ep["fast_cancel"] = True
    for ep in open_ep.values():
        ep["end"] = ep["start"] + 60.0             # still open at the window end
        ep["how"] = ep["how"] or "open"
    return eps


def load_fills(d, since, until):
    by_order = collections.defaultdict(list)
    for p in sorted(glob.glob(os.path.join(d, "fills_*.jsonl"))):
        try:
            day = datetime.strptime(os.path.basename(p)[6:16], "%Y-%m-%d").date()
        except ValueError:
            continue
        if not (datetime.fromtimestamp(since, timezone.utc).date() - timedelta(days=2)
                <= day <= datetime.fromtimestamp(until, timezone.utc).date() + timedelta(days=1)):
            continue
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("is_taker") or not r.get("order_id"):
                    continue
                ts = float(r.get("ts") or 0)
                if since - 5 <= ts <= until + 5:
                    by_order[r["order_id"]].append(r)
    return by_order


def load_markouts(d):
    """(int ts, ticker) -> [fills with m5c / m4c] from the dashboard object."""
    p = os.path.join(d, "dashboard", "imm_dashboard.html")
    out = collections.defaultdict(list)
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("const D"):
                    D = json.loads(s[s.index("=") + 1:].rstrip(";"))
                    for fl in D.get("fills") or []:
                        out[(int(fl["ts"]), fl["t"])].append(fl)
                    break
    except (OSError, ValueError):
        pass
    return out


def load_reward_rates(d):
    """(ticker, UTC hour index) -> our estimated reward that hour."""
    rates = {}
    try:
        with open(os.path.join(d, "reward_est_hourly_cache.json"), encoding="utf-8") as f:
            cache = json.load(f)
        for _day, v in cache.items():
            for t, hrs in (v.get("markets") or {}).items():
                for h, x in hrs.items():
                    rates[(t, int(h))] = float(x)
    except (OSError, ValueError, AttributeError):
        pass
    return rates


def markout(fill, marks):
    n = float(fill.get("count") or 0)
    for c in marks.get((int(fill["ts"]), fill.get("ticker")), ()):
        if abs(float(c.get("n") or 0) - n) < 0.02:
            m5 = c.get("m5c")
            m4 = c.get("m4c")
            return (None if m5 is None else m5 * n / 100.0,
                    None if m4 is None else m4 * n / 100.0)
    return None, None


def score(eps, fills, marks, lo_s=0.0, hi_s=None):
    """Fills on each episode's order from first sight + lo_s to its end (or to
    first sight + hi_s): count, contracts, 5-min and 4-hour $ (+ = ours)."""
    out = {"fills": 0, "cts": 0.0, "m5": 0.0, "m4": 0.0, "marked": 0}
    for ep in eps:
        a = ep["start"] + lo_s
        b = ep["end"] + GRACE_S if hi_s is None else ep["start"] + hi_s
        for fl in fills.get(ep["oid"], ()):
            if a <= float(fl["ts"]) <= b:
                out["fills"] += 1
                out["cts"] += float(fl.get("count") or 0)
                m5, m4 = markout(fl, marks)
                if m4 is not None:
                    out["marked"] += 1
                    out["m4"] += m4
                    out["m5"] += m5 or 0.0
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--since", help="UTC ISO; default the first `seen` line")
    ap.add_argument("--until", help="UTC ISO; default now")
    args = ap.parse_args(argv)
    d = args.dir
    until = _ts(args.until) if args.until else datetime.now(timezone.utc).timestamp()
    since = _ts(args.since) if args.since else until - 4 * 86400
    rows = load_stale(d, since, until)
    if not args.since:
        seen0 = next((r["ts"] for r in rows if r.get("ev") == "seen"), None)
        if seen0 is not None:
            since = seen0
            rows = [r for r in rows if r["ts"] >= since - 5]
    days = max((until - since) / 86400.0, 1e-9)
    eps = episodes_from(rows)
    fills = load_fills(d, since, until)
    marks = load_markouts(d)
    rates = load_reward_rates(d)
    acting = [e for e in eps if e["acts"]]
    arms = {a: [e for e in acting if e["arm"] == a] for a in ("fast", "control")}
    L = []
    w = L.append
    t0 = datetime.fromtimestamp(since, ET).strftime("%a %m/%d %H:%M")
    t1 = datetime.fromtimestamp(until, ET).strftime("%a %m/%d %H:%M ET")
    w(f"# Faster fast cancel: {days:.1f}-day report ({t0} to {t1})")
    w("")
    if not eps:
        w("No `seen` lines in the window: the first-sighting log never ran "
          "(IMM_WS_SEEN_LOG / IMM_WS_STALE_LOG off, or the deploy did not land).")
        return finish(d, L)
    # --- the two arms ---------------------------------------------------
    res = {}
    for a, lst in arms.items():
        sc = score(lst, fills, marks)
        cancels = [e for e in lst if e["fast_cancel"]]
        flick = [e for e in lst if (e["how"] or "").startswith("unseen:clear")]
        secs_to_cancel = [r.get("seen_s") for r in rows
                          if r.get("ev") == "cancel" and r.get("by") == "fast"
                          and r.get("arm") == a and r.get("seen_s") is not None]
        res[a] = {"n": len(lst), "sc": sc, "cancels": len(cancels),
                  "flick": len(flick),
                  "med_cancel_s": statistics.median(secs_to_cancel) if secs_to_cancel else None,
                  "cancel_eps": cancels}
    f, c = res["fast"], res["control"]
    per = lambda r, k: (r["sc"][k] / r["n"] * 1000.0) if r["n"] else 0.0
    # loss avoided by speed: the control's fill $ per episode applied to the
    # fast arm's episodes, minus what the fast arm actually took
    avoided = (per(f, "m4") - per(c, "m4")) / 1000.0 * f["n"]
    avoided5 = (per(f, "m5") - per(c, "m5")) / 1000.0 * f["n"]
    # reward the EXTRA cancels gave up: the fast arm cancels a share of
    # episodes the control lets clear on its own
    extra_share = max(0.0, (f["cancels"] / f["n"] if f["n"] else 0.0)
                      - (c["cancels"] / c["n"] if c["n"] else 0.0))
    rew_lost_all = 0.0
    for e in f["cancel_eps"]:
        rate = rates.get((e["t"], int(e["start"] // 3600)), 0.0) / 3600.0 / 2.0
        rew_lost_all += rate * OFF_BOOK_S
    share_extra = (extra_share / (f["cancels"] / f["n"])) if f["n"] and f["cancels"] else 0.0
    rew_lost = rew_lost_all * share_extra
    net = avoided - rew_lost
    verdict = ("KEEP" if net >= 0 and f["n"] >= 50 else
               "WATCH (too few episodes to call)" if f["n"] < 50 else "ROLL BACK")
    w(f"**Verdict: {verdict}.** Net {net:+.2f} over the window ({net / days:+.2f}/day): "
      f"fills avoided {avoided:+.2f} at 4 hours ({avoided5:+.2f} at 5 min) minus "
      f"{rew_lost:.2f} of reward the extra cancels gave up.")
    w("")
    w("Acting episodes (live, at least the 10c gate), by arm:")
    w("")
    w("| | fast (0.25s) | control (1s) |")
    w("|---|---|---|")
    w(f"| episodes | {f['n']} ({f['n'] / days:.0f}/day) | {c['n']} ({c['n'] / days:.0f}/day) |")
    w(f"| fast cancels | {f['cancels']} | {c['cancels']} |")
    med = lambda r: "-" if r["med_cancel_s"] is None else "%.2f" % r["med_cancel_s"]
    w(f"| median seconds from first sight to cancel | {med(f)} | {med(c)} |")
    w(f"| ended on their own before confirming (flickers) | {f['flick']} | {c['flick']} |")
    w(f"| fills during the episode | {f['sc']['fills']} ({f['sc']['cts']:.0f} ct) | "
      f"{c['sc']['fills']} ({c['sc']['cts']:.0f} ct) |")
    w(f"| their 4-hour mark-out, $ per 1,000 episodes | {per(f, 'm4'):+.1f} | {per(c, 'm4'):+.1f} |")
    w(f"| their 5-min mark-out, $ per 1,000 episodes | {per(f, 'm5'):+.1f} | {per(c, 'm5'):+.1f} |")
    w("")
    w(f"The arms are drawn at random when the bot first sees an order left "
      f"ahead, so the per-episode rates compare directly. Extra cancels: "
      f"{extra_share:.1%} of fast-arm episodes; each sits off the book about "
      f"{OFF_BOOK_S:.0f}s until the next cycle re-places it (at the back of the line).")
    w("")
    # --- the gaps the gate leaves alone -------------------------------------
    w("## The 1-9c gaps (logged, never cancelled)")
    w("")
    w("| gap | first sightings/day | ended within 1s on their own | fills 0.25-1.25s after first sight | their 4h $ | fills after 1.25s | their 4h $ |")
    w("|---|---|---|---|---|---|---|")
    for lab, lo, hi in (("1-2c", 0.0, 3.0), ("3-5c", 3.0, 6.0), ("6-9c", 6.0, 10.0)):
        grp = [e for e in eps if not e["acts"] and e["gap"] is not None and lo <= e["gap"] < hi]
        if not grp:
            w(f"| {lab} | 0 | - | - | - | - | - |")
            continue
        quick = sum(1 for e in grp if (e["how"] or "").startswith("unseen")
                    and (e["end"] - e["start"]) <= 1.0)
        early = score(grp, fills, marks, lo_s=0.25, hi_s=1.25)
        late = score(grp, fills, marks, lo_s=1.25)
        w(f"| {lab} | {len(grp) / days:.0f} | {quick / len(grp):.0%} | "
          f"{early['fills']} | {early['m4']:+.0f} | {late['fills']} | {late['m4']:+.0f} |")
    w("")
    w("A fill 0.25-1.25s after first sight is one a 0.25s cancel at that gap "
      "could have prevented; after 1.25s, the old 1s rule would have too. "
      "Flickers that end within 1s are the cancels it would have spent for nothing.")
    w("")
    marked = sum(r["sc"]["marked"] for r in res.values())
    total = sum(r["sc"]["fills"] for r in res.values())
    w(f"Data: {len(eps)} first sightings, {len(rows)} ws_stale lines; {marked} of "
      f"{total} acting-episode fills had dashboard mark-outs. Scored by "
      f"imm_fast_cancel_report.py.")
    return finish(d, L)


def finish(d, lines):
    name = f"fast_cancel_report_{datetime.now(ET).strftime('%Y-%m-%d')}.md"
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
