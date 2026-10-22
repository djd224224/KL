#!/usr/bin/env python3
r"""Daily email of the 15-minute shadow test (Q15_SHADOW.md; verdict rules in
Q15_PREREG.md, fixed before the data). Jack 2026-10-22: "Yes use a live price
feed like Miami temp, with recorder and a week of testing" / "It's paid, do FX
and crypto".

Sims yesterday's ET day (q15_sim.simulate_day), then reports:
  yesterday  per variant: net = reward + fill P&L at settlement (STRESS
             fills), the night share, fills
  running    since VERDICT_START: net, positive days, the robust total (best
             and worst hour dropped), a 90% bootstrap CI over days
  checks     the FX proxy's up / down call against Kalshi's results; the
             fair's Brier against the market mid's; recording coverage
  verdict    per variant: PILOT ELIGIBLE / EXTEND / NO-GO by the
             pre-registered rules (provisional before 7 full days)
Writes reports/q15_<day>.html and sends it through send_html_email.py (the
ALERT_EMAIL_* account, like the hourly-temp report). Read-only otherwise.

usage: python q15_report.py [--day YYYY-MM-DD] [--no-sim] [--send]
Scheduled daily 08:45 ET as "KL q15-shadow report" (register_q15_report.ps1).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import q15_sim as Q  # noqa: E402  (points temp_shadow_common at the q15 folder)

import argparse  # noqa: E402
import html  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import random  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402

import temp_shadow_common as C  # noqa: E402

VERDICT_START = date(2026, 10, 23)
VERDICT_DAYS = 7
MIN_POSITIVE_DAYS = 5
CI_FLOOR = -50.0
SEND_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "send_html_email.py")


def load_day(d: date):
    return C.read_json(os.path.join(Q.DATA, "sim", d.isoformat(), "q15_day.json"))


def window_days(day: date) -> list:
    out, d = [], VERDICT_START
    while d <= day:
        js = load_day(d)
        if js:
            out.append(js)
        d += timedelta(days=1)
    return out


def bootstrap_lo(xs: list, n: int = 4000, q: float = 0.05, seed: int = 15) -> float:
    """The q-quantile of the bootstrap distribution of the SUM over days."""
    if not xs:
        return 0.0
    rnd = random.Random(seed)
    sums = sorted(sum(rnd.choice(xs) for _ in xs) for _ in range(n))
    return sums[int(q * n)]


def brier_overall(days: list) -> tuple:
    """(fair Brier, mid Brier, n) pooled over the days' series and times."""
    f = m = n = 0.0
    for d in days:
        for v in (d.get("brier") or {}).values():
            f += v["fair"] * v["n"]
            m += v["mid"] * v["n"]
            n += v["n"]
    return (f / n, m / n, int(n)) if n else (None, None, 0)


def verdict(vid: str, days: list) -> dict:
    nets = [d["variants"][vid]["net"] for d in days if vid in d.get("variants", {})]
    hours = []
    for d in days:
        hours += list((d["variants"].get(vid) or {}).get("by_hour", {}).values())
    total = sum(nets)
    robust = sum(hours) - (max(hours) + min(hours)) if len(hours) >= 3 else total
    pos = sum(1 for x in nets if x > 0)
    lo = bootstrap_lo(nets)
    bf, bm, bn = brier_overall(days)
    checks = {"net > 0": total > 0,
              f">= {MIN_POSITIVE_DAYS}/{VERDICT_DAYS} days positive":
                  pos >= MIN_POSITIVE_DAYS if len(nets) >= VERDICT_DAYS
                  else pos >= math.ceil(MIN_POSITIVE_DAYS / VERDICT_DAYS * len(nets)),
              "robust total > 0": robust > 0,
              f"90% CI low > ${CI_FLOOR:.0f}": lo > CI_FLOOR,
              "fair beats the mid (Brier)": bf is not None and bm is not None and bf < bm}
    first4 = all(list(checks.values())[:4])
    if not nets:
        level = "NO DATA"
    elif len(nets) >= VERDICT_DAYS and all(checks.values()):
        level = "PILOT ELIGIBLE"
    elif len(nets) < VERDICT_DAYS and first4:
        level = "EXTEND"
    else:
        level = "NO-GO"
    return {"vid": vid, "days": len(nets), "total": total, "robust": robust, "pos": pos,
            "ci_lo": lo, "checks": checks, "level": level}


def usd(x: float) -> str:
    x = round(float(x), 2) + 0.0
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def build(day: date) -> tuple:
    """(subject, html, text) for the report of `day`."""
    today = load_day(day) or {}
    days = window_days(day)
    vs = list(Q.VARIANTS)
    verdicts = {v: verdict(v, days) for v in vs}
    n_days = len(days)
    best = max(vs, key=lambda v: verdicts[v]["total"]) if days else vs[0]
    subj = (f"Q15 shadow {day.strftime('%m/%d')}: best {best} {usd(verdicts[best]['total'])} over "
            f"{n_days} day(s) | {verdicts[best]['level']}")
    lines = [f"Q15 shadow test -- {day.strftime('%a %Y-%m-%d')} (ET), day {n_days} of {VERDICT_DAYS} "
             f"(verdict window from {VERDICT_START.isoformat()})", ""]
    if today:
        lines.append(f"Yesterday: {today.get('markets', 0)} markets ({today.get('settled', 0)} settled, "
                     f"{today.get('with_program', 0)} paid). STRESS fills, marked to Kalshi's result:")
        for v in vs:
            r = today["variants"].get(v) or {}
            lines.append(f"  {v:12s} net {usd(r.get('net', 0)):>10s} = reward {usd(r.get('reward', 0)):>9s}"
                         f" + fills {usd(r.get('pnl', 0)):>10s}  ({r.get('fills', 0)} fills; night "
                         f"{usd(r.get('night', 0))})")
        unsettled = max((r.get("unsettled", 0) for r in today["variants"].values()), default=0)
        if unsettled:
            lines.append(f"  ({unsettled} market(s) not settled yet: their fills count $0 until "
                         f"Kalshi's result is recorded.)")
        fid = today.get("fidelity") or {}
        if fid.get("n"):
            lines.append(f"  FX proxy (Kraken) up/down call agreed with Kalshi on {fid['agree']} of "
                         f"{fid['n']} closes ({100.0 * fid['agree'] / fid['n']:.0f}%).")
        cov = today.get("coverage") or {}
        lines.append(f"  Recording: {cov.get('ws_rows', 0):,} ws rows, {cov.get('trades', 0):,} trades, "
                     f"{cov.get('untrusts', 0)} reconnect/gap resets.")
    else:
        lines.append("Yesterday: no sim output (no recording?).")
    lines += ["", f"Running since {VERDICT_START.strftime('%m/%d')} ({n_days} day(s)):"]
    for v in vs:
        r = verdicts[v]
        lines.append(f"  {v:12s} {usd(r['total']):>10s}  days + {r['pos']}/{r['days']}  robust "
                     f"{usd(r['robust']):>10s}  CI-low {usd(r['ci_lo']):>10s}  -> {r['level']}")
    bf, bm, bn = brier_overall(days)
    if bn:
        lines.append(f"  Brier (lower is better), {bn} points: fair {bf:.4f} vs market mid {bm:.4f}"
                     f" -> {'the fair adds information' if bf < bm else 'the fair does NOT beat the mid'}")
    lines += ["", "Rules (Q15_PREREG.md, fixed 10/22 before the data): PILOT ELIGIBLE needs net > 0, "
              f">= {MIN_POSITIVE_DAYS}/{VERDICT_DAYS} days positive, the robust total > 0, a 90% CI low "
              f"> ${CI_FLOOR:.0f}, and the fair beating the mid; EXTEND = passing so far with fewer "
              f"than {VERDICT_DAYS} days. A pilot would be Jack's call (1/3 size, its own hours, a "
              "$200 daily halt, a live-vs-shadow breaker). Nothing trades."]
    text = "\n".join(lines)
    page = ("<!doctype html><html><head><meta charset='utf-8'><style>body{font:14px/1.45 "
            "-apple-system,Segoe UI,Arial,sans-serif;color:#1b1b1b;background:#fff} pre{white-space:"
            "pre-wrap;font:13px/1.4 Consolas,monospace}</style></head><body><pre>"
            + html.escape(text) + "</pre></body></html>")
    return subj, page, text


def send(subject: str, page_path: str, text: str) -> bool:
    for attempt in range(3):
        try:
            r = subprocess.run([sys.executable, SEND_SCRIPT, subject, page_path, text],
                               timeout=180, capture_output=True, text=True)
            if r.returncode == 0:
                return True
            print(f"send attempt {attempt + 1} failed: {r.stderr.strip()[:300]}")
        except Exception as e:
            print(f"send attempt {attempt + 1} failed: {e}")
        time.sleep(60)
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", help="ET day YYYY-MM-DD (default yesterday)")
    ap.add_argument("--no-sim", action="store_true", help="use the existing sim output")
    ap.add_argument("--send", action="store_true", help="email it (else print only)")
    args = ap.parse_args(argv)
    now_et = datetime.now(Q.ET)
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else now_et.date() - timedelta(days=1))
    if not args.no_sim:
        Q.simulate_day(day, echo=True)
    subject, page, text = build(day)
    path = os.path.join(Q.DATA, "reports", f"q15_{day.isoformat()}.html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    print(subject)
    print(text)
    if args.send:
        ok = send(subject, path, text)
        print(f"q15 report send: {'ok' if ok else 'FAILED'}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
