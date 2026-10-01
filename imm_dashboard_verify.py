#!/usr/bin/env python3
r"""imm_dashboard_verify.py -- re-derive the dashboard's trading P&L from
Kalshi's own records and say whether they agree.

For each window it builds the dashboard's model in-process, then replays the
window independently of the bot's realized/avg-cost bookkeeping:

  * start positions  = the bot's 5-minute snapshot at the window's start edge
  * fills            = Kalshi GET /portfolio/fills, kept when the order id is
                       the bot's (orders-log `place` rows + state our_order_ids)
  * exits            = every position gone from the end snapshot, settled at
                       Kalshi's market result (yes / no / scalar value); one
                       Kalshi has not settled is a transfer at its last mark
  * P&L              = cash from fills + settlement payouts + V1 - V0

and checks that the fills log holds exactly Kalshi's fills and that the two
P&L figures match market by market. A market whose end position the replay
cannot reproduce had the bot's own book edited without a fill (a restore, a
reconcile adoption); it is listed apart, since the dashboard follows the
bot's book. Fills are placed by the cycle that booked them (the fills log's
cycle_ts), as the dashboard places realized P&L.
Read-only. Found on 2026-09-29: scalar settlements the bot logs as manual
offsets (+$299 unbooked 9/6-9/29), positions dropped with no record, and
double-written fill rows -- all now handled by the dashboard.

USAGE:
  python imm_dashboard_verify.py                 # yesterday, today, 7d
  python imm_dashboard_verify.py yesterday 24h
Exit code 0 when every window agrees within a cent per market, 1 otherwise.
"""
import json
import os
import sys
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import imm_dashboard as dash                        # noqa: E402
from kalshi_reads import kalshi_get, kalshi_get_all   # noqa: E402

TOL = 0.01


def bot_order_ids(e0: float, e1: float) -> set:
    ids = set(dash.load_json(os.path.join(dash.STATUS_DIR, "imm_state.json")).get("our_order_ids") or {})
    days = sorted({time.strftime("%Y-%m-%d", time.gmtime(t))
                   for t in (e0 - 2 * 86400, e0 - 86400, e0, e1, e1 + 3600)})
    for day in days:
        p = os.path.join(dash.STATUS_DIR, f"orders_{day}.jsonl")
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"kind": "place"' not in line:
                    continue
                try:
                    ids.add(json.loads(line)["order_id"])
                except (ValueError, KeyError):
                    pass
    return ids


def verify(b: "dash.Builder", key: str) -> bool:
    w = b.windows[key]
    mk, (e0, e1, ok) = b.window_markets(w["start"], w["end"])
    if not ok:
        print(f"{key}: no position snapshot at the window's start; skipped")
        return True
    s0, s1 = b.snap_at(e0), b.snap_at(e1)
    ids = bot_order_ids(e0, e1)
    kf = kalshi_get_all("/portfolio/fills", {"min_ts": int(e0) - 900, "max_ts": int(e1) + 900, "limit": 1000},
                        items_key="fills", max_pages=400)
    # a fill belongs to the window whose end snapshot first holds it: by the
    # cycle that booked it (the fills log's cycle_ts), as the dashboard does
    booked = {f["id"]: (f["cyc"] - b.FILL_BOOK_LAG if f.get("cyc") else f["ts"]) for f in b.fills}
    def when(f):
        return booked.get(f["fill_id"], f["ts"])
    bot = [f for f in kf if e0 <= when(f) < e1 and f.get("order_id") in ids]
    sink = {f["id"] for f in b.fills if e0 <= booked.get(f["id"], f["ts"]) < e1}
    kid = {f["fill_id"] for f in bot}
    pos = {t: v[0] for t, v in s0[1].items()}
    cash = defaultdict(float)
    for f in sorted(bot, key=when):
        q = float(f["count_fp"])
        dq = q if f["book_side"] == "bid" else -q
        pos[f["ticker"]] = pos.get(f["ticker"], 0.0) + dq
        cash[f["ticker"]] -= dq * float(f["yes_price_dollars"])
    gone = [t for t in pos if abs(pos[t]) > 0.005 and t not in s1[1]]
    res = {}
    for i in range(0, len(gone), 50):
        for m in kalshi_get("/markets", {"tickers": ",".join(gone[i:i + 50]), "limit": 50}).get("markets") or []:
            res[m["ticker"]] = m
    payout = defaultdict(float)
    for t in gone:
        m = res.get(t, {})
        r = str(m.get("result") or "").lower()
        if r in ("yes", "no", "scalar"):
            px = 1.0 if r == "yes" else 0.0 if r == "no" else float(m.get("settlement_value_dollars") or 0)
        else:                                             # still open: out at the last mark
            px = next((e["mark"] / 100.0 for e in reversed(b.exits) if e["t"] == t), 0.0)
        payout[t] += pos[t] * px
        pos[t] = 0.0
    per = defaultdict(float)
    for t, (p, a, m_) in s0[1].items():
        per[t] -= p * (m_ if m_ is not None else a) / 100.0
    for t, (p, a, m_) in s1[1].items():
        per[t] += pos.get(t, 0.0) * (m_ if m_ is not None else a) / 100.0
    for t, v in cash.items():
        per[t] += v
    for t, v in payout.items():
        per[t] += v
    replay = sum(per.values())
    shown = sum(v["pnl"] for v in mk.values())
    # a market whose end position the replay cannot reproduce had its OWN BOOK
    # changed without a fill on the bot's orders (a state restore, a reconcile
    # adopting someone else's fill): the dashboard follows the bot's book, so
    # it is reported apart from method errors
    edited = {t for t in set(pos) | set(s1[1])
              if abs(pos.get(t, 0.0) - (s1[1][t][0] if t in s1[1] else 0.0)) > 0.05}
    worst = sorted(set(per) | set(mk), key=lambda t: -abs(per.get(t, 0.0) - mk.get(t, {}).get("pnl", 0.0)))
    diff = [t for t in worst if abs(per.get(t, 0.0) - mk.get(t, {}).get("pnl", 0.0)) > TOL]
    bad = [t for t in diff if t not in edited]
    good = not bad and kid == sink
    print(f"{key:9s} {'OK  ' if good else 'DIFF'} dashboard {shown:+,.2f} vs Kalshi replay {replay:+,.2f} | "
          f"fills {len(sink)} logged / {len(kid)} on Kalshi (missing {len(kid - sink)}, extra {len(sink - kid)})"
          + (f" | bot book edited without a fill on {len(edited)} market(s)" if edited else ""))
    for t in bad[:8]:
        print(f"          {t:50s} dashboard {mk.get(t, {}).get('pnl', 0.0):+9.2f}  replay {per.get(t, 0.0):+9.2f}")
    for t in sorted(edited)[:8]:
        print(f"          edited: {t:42s} dashboard {mk.get(t, {}).get('pnl', 0.0):+9.2f}  replay {per.get(t, 0.0):+9.2f}"
              f"  (bot book {s1[1][t][0] if t in s1[1] else 0.0:+.2f} vs fills {pos.get(t, 0.0):+.2f})")
    return good


def main(argv=None) -> int:
    keys = (argv if argv is not None else sys.argv[1:]) or ["yesterday", "today", "7d"]
    b = dash.Builder(time.time(), api=True, api_force=False)
    b.load()
    b.build_exits()
    ok = True
    for k in keys:
        if k not in b.windows:
            print(f"unknown window {k!r}; choose from {', '.join(b.windows)}")
            return 2
        ok = verify(b, k) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
