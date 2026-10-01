"""CPI core pilot report (read-only): does quoting KXCPICORE-26NOV/-26DEC
pay? Rewards (credited, else the bot's own estimate) against the pilot's
own trading: every fill since the pilot went live, mark-outs at +1h / +24h
/ now from hourly candle mids, and the mid-marked P&L of what it holds.

Written 2026-09-28 for the Oct 13 review (Jack: "keep it default running,
dont block but give me a report on oct 13. ill block if needed").

    python imm_cpi_pilot_report.py [--since 2026-09-29T02:00:00Z]
                                   [--events KXCPICORE-26NOV,KXCPICORE-26DEC]
                                   [--families KXCPICOREYOY]

Prints Markdown and writes run-logs/incentive-mm/cpi_pilot_report_<date>.md.
Places, amends and cancels nothing.

Fill semantics (verified 2026-09-28, see the digest settlement fix): trust
`book_side` -- a bid fill adds YES at yes_price, an ask fill removes YES at
yes_price (opening NO at 1 - yes when none is held); YES/NO pairs redeem
for $1 at the fill that nets them.
"""
import argparse
import csv
import glob
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import pytz

KL = os.path.dirname(os.path.abspath(__file__))
if KL not in sys.path:
    sys.path.insert(0, KL)
from crypto_touch_mm import build_client  # noqa: E402

STATUS = os.environ.get("IMM_STATUS_DIR", os.path.join(KL, "run-logs", "incentive-mm"))
ET = pytz.timezone("US/Eastern")
F = lambda v: float(v or 0.0)                                   # noqa: E731
# the stint this pilot is judged against (9/14-9/24 open-scan CPI quoting)
BASELINE = {"markout_1h": -7.64, "markout_24h": -14.33, "credits": 95.71,
            "trading": -210.31, "fills": 39, "contracts": 1110}
# accrued_est is a lifetime counter: what the KXCPICOREYOY tickers carried
# from that stint when the family joined the pilot (imm_state.json,
# 2026-10-01 17:30Z, before the deploy -- blocked, so frozen) is not the
# pilot's and comes off their estimate
PRE_PILOT_ACCRUED = {"KXCPICOREYOY-26DEC-T2.5": 4.0907, "KXCPICOREYOY-26DEC-T2.8": 4.9402,
                     "KXCPICOREYOY-26DEC-T2.9": 7.2366, "KXCPICOREYOY-26NOV-T2.5": 5.0404}


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-29T02:00:00Z")
    ap.add_argument("--events", default="KXCPICORE-26NOV,KXCPICORE-26DEC")
    # whole families in the pilot (Jack 2026-10-01: "yes add this family"):
    # their open events plus any event the IMM's ledgers show a fill in
    ap.add_argument("--families", default="KXCPICOREYOY")
    ap.add_argument("--out", default=None,
                    help="report path (default run-logs/incentive-mm/cpi_pilot_report_<ET date>.md)")
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(errors="replace")      # cp1252 consoles
    except (AttributeError, ValueError):
        pass
    since = parse_ts(a.since)
    events = [e.strip() for e in a.events.split(",") if e.strip()]
    now = datetime.now(timezone.utc)
    c = build_client()

    def get(path, params=None):
        for i in range(6):
            try:
                return c.get(path, params=params)
            except Exception as e:                            # noqa: BLE001
                if "429" in str(e):
                    time.sleep(1.5 * (i + 1))
                    continue
                raise
        raise RuntimeError(path)

    for fam in [s.strip() for s in a.families.split(",") if s.strip()]:
        fam_events = set()
        for fp in glob.glob(os.path.join(STATUS, "fills_*.jsonl")):
            with open(fp, encoding="utf-8") as fh:
                for line in fh:
                    if f'"series": "{fam}"' in line:
                        try:
                            fam_events.add(json.loads(line)["event_ticker"])
                        except (ValueError, KeyError):
                            pass
        for e in get("/events", {"series_ticker": fam, "status": "open",
                                 "limit": 200}).get("events") or []:
            if e.get("event_ticker"):
                fam_events.add(e["event_ticker"])
        events += sorted(ev for ev in fam_events if ev not in events)

    state = {}
    try:
        with open(os.path.join(STATUS, "imm_state.json"), encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        pass
    accrued = state.get("accrued_est") or {}
    # The IMM's own fills, from its permanent per-day ledgers (imm_state's
    # seen_fill_ids is a pruned rolling window -- 28 of the 9/14 stint's 39
    # fills were already gone from it on 9/28).
    imm_ids = set()
    for fp in glob.glob(os.path.join(STATUS, "fills_*.jsonl")):
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                if not any(e in line for e in events):
                    continue
                try:
                    fid = json.loads(line).get("fill_id")
                except ValueError:
                    continue
                if fid:
                    imm_ids.add(fid)

    rows, fills_all = [], []
    tot = defaultdict(float)
    for ev in events:
        ms = get("/markets", {"event_ticker": ev, "limit": 100}).get("markets") or []
        for m in sorted(ms, key=lambda m: m["ticker"]):
            t = m["ticker"]
            fs, cur = [], None
            while True:
                p = {"ticker": t, "min_ts": int(since.timestamp()), "limit": 200}
                if cur:
                    p["cursor"] = cur
                r = get("/portfolio/fills", p)
                fs += r.get("fills") or []
                cur = r.get("cursor")
                if not cur:
                    break
            fs.sort(key=lambda f: int(f["ts"]))
            Y = N = cash = 0.0
            for f in fs:
                q, py = F(f["count_fp"]), F(f["yes_price_dollars"])
                if f["book_side"] == "bid":
                    Y += q
                    cash -= q * py
                else:
                    s_ = min(Y, q)
                    Y -= s_
                    cash += s_ * py
                    N += q - s_
                    cash -= (q - s_) * (1 - py)
                cash -= F(f.get("fee_cost"))
                pr = min(Y, N)
                Y -= pr
                N -= pr
                cash += pr
            b, ask = F(m.get("yes_bid_dollars")), F(m.get("yes_ask_dollars"))
            res = (m.get("result") or "").lower()
            if res in ("yes", "no"):
                mid = 1.0 if res == "yes" else 0.0
            elif b > 0 and ask > 0:
                mid = (b + ask) / 2
            else:
                mid = F(m.get("last_price_dollars"))
            val = Y * mid + N * (1 - mid)
            pts = []
            if fs:
                ser = t.split("-")[0]
                cs = get(f"/series/{ser}/markets/{t}/candlesticks",
                         {"start_ts": int(fs[0]["ts"]) - 3600,
                          "end_ts": int(now.timestamp()),
                          "period_interval": 60}).get("candlesticks") or []
                for x in cs:
                    bb = F((x.get("yes_bid") or {}).get("close_dollars"))
                    aa = F((x.get("yes_ask") or {}).get("close_dollars"))
                    if bb > 0 and aa > 0:
                        pts.append((int(x["end_period_ts"]), (bb + aa) / 2))

            def mid_at(ts, pts=pts):
                best = None
                for e_, v in pts:
                    if e_ <= ts + 3600:
                        best = v
                return best
            for f in fs:
                q, py, ts = F(f["count_fp"]), F(f["yes_price_dollars"]), int(f["ts"])
                sg = 1 if f["book_side"] == "bid" else -1
                m1 = mid_at(ts + 3600) if ts + 3600 <= now.timestamp() else None
                m24 = mid_at(ts + 86400) if ts + 86400 <= now.timestamp() else None
                et = datetime.fromtimestamp(ts, timezone.utc).astimezone(ET)
                fills_all.append({
                    "t": t, "q": q, "side": f["book_side"], "px": py, "et": et,
                    "imm": f["fill_id"] in imm_ids if imm_ids else None,
                    "m1": None if m1 is None else sg * (m1 - py),
                    "m24": None if m24 is None else sg * (m24 - py),
                    "mnow": sg * (mid - py),
                    "blackout": et.weekday() < 5 and (8 * 60 + 25) <= et.hour * 60 + et.minute < (11 * 60 + 5)})
            est = max(0.0, F(accrued.get(t)) - PRE_PILOT_ACCRUED.get(t, 0.0))
            rows.append((t, len(fs), sum(F(f["count_fp"]) for f in fs), Y - N,
                         cash, val, cash + val, est, mid))
            tot["cash"] += cash
            tot["val"] += val
            tot["est"] += est

    credits, credit_rows, credits_asof = 0.0, 0, None
    cpath = os.path.join(STATUS, "reward_credits.csv")
    if os.path.exists(cpath):
        credits_asof = datetime.fromtimestamp(os.path.getmtime(cpath)).strftime("%Y-%m-%d %H:%M")
        with open(cpath, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("event_ticker") in events and r.get("credit_date", "") >= since.strftime("%Y-%m-%d"):
                    credits += F(r.get("amount"))
                    credit_rows += 1

    def wavg(key, sel):
        ok = [(x["q"], x[key]) for x in sel if x[key] is not None]
        qn = sum(q for q, _ in ok)
        return (100 * sum(q * v for q, v in ok) / qn, qn) if qn else (None, 0)

    days = max((now - since).total_seconds() / 86400, 1e-9)
    trading = tot["cash"] + tot["val"]
    reward = credits if credit_rows else tot["est"]
    L = []
    L.append(f"# CPI core pilot report — {now.astimezone(ET):%Y-%m-%d %H:%M} ET")
    L.append("")
    L.append(f"Pilot: {', '.join(events)} since {since:%Y-%m-%d %H:%MZ} ({days:.1f} days).")
    L.append("")
    L.append("## Money")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| Rewards credited by Kalshi (reward_credits.csv, updated {credits_asof or 'n/a'}) | "
             f"${credits:,.2f} ({credit_rows} rows) |")
    L.append(f"| Rewards the bot estimates it accrued (accrued_est) | ${tot['est']:,.2f} |")
    L.append(f"| Trading P&L, marked at mid (cash {tot['cash']:+,.2f}, holdings {tot['val']:,.2f}) | ${trading:+,.2f} |")
    L.append(f"| **Net** (using {'credits' if credit_rows else 'the estimate — no credits on file yet'}) | "
             f"**${reward + trading:+,.2f}** (${(reward + trading) / days:+,.2f}/day) |")
    L.append("")
    fl = fills_all
    L.append("## Fills and mark-outs (¢ per contract, + = in our favor)")
    L.append("")
    L.append(f"{len(fl)} fills, {sum(x['q'] for x in fl):,.0f} contracts"
             + (f"; {sum(1 for x in fl if x['imm'] is False)} not from the IMM" if imm_ids else ""))
    L.append("")
    L.append("| slice | fills | +1h | +24h | now |")
    L.append("|---|---|---|---|---|")
    for label, sel in (("all", fl),
                       ("in the 08:25-11:05 ET weekday blackout (should be 0)", [x for x in fl if x["blackout"]]),
                       ("bids (bot bought YES)", [x for x in fl if x["side"] == "bid"]),
                       ("asks (bot sold YES)", [x for x in fl if x["side"] == "ask"])):
        cells = []
        for k in ("m1", "m24", "mnow"):
            v, _ = wavg(k, sel)
            cells.append("—" if v is None else f"{v:+.1f}")
        L.append(f"| {label} | {len(sel)} | " + " | ".join(cells) + " |")
    L.append(f"| baseline: 9/14-9/24 CPI stint | {BASELINE['fills']} | {BASELINE['markout_1h']:+.1f} | "
             f"{BASELINE['markout_24h']:+.1f} | — |")
    L.append("")
    L.append("## By market")
    L.append("")
    L.append("| market | fills | contracts | net pos | mid now | trading P&L | est reward |")
    L.append("|---|---|---|---|---|---|---|")
    for t, n, q, pos, cash, val, pnl, est, mid in rows:
        if n or est:
            L.append(f"| {t} | {n} | {q:,.0f} | {pos:+,.0f} | {100 * mid:.1f}¢ | ${pnl:+,.2f} | ${est:,.2f} |")
    L.append("")
    m24, _ = wavg("m24", fl)
    L.append("## Bar set at launch")
    L.append("")
    L.append("Keep if fills lose no more than ~2¢/contract a day later, or rewards beat the trading loss. "
             f"This run: 24h mark-out {'n/a' if m24 is None else f'{m24:+.1f}¢'}, "
             f"rewards ${reward:,.2f} vs trading ${trading:+,.2f}. Blocking is Jack's call.")
    out = "\n".join(L)
    print(out)
    path = a.out or os.path.join(STATUS, f"cpi_pilot_report_{now.astimezone(ET):%Y-%m-%d}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(out + "\n")
    print(f"\n(written to {path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
