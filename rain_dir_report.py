#!/usr/bin/env python3
"""Rain-directional performance report (Jack 2026-07-28).

Joins run-logs/incentive-mm/rain_directional_ledger.csv against Kalshi
market results and prints per-bet outcomes, daily rollups, hit rate and
NWS-fair calibration. Open positions (unsettled markets) are marked OPEN
with mark-to-market against the current mid.

Run anytime:  python rain_dir_report.py
"""
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("KALSHI_RATE_LIMIT_MS", "50")
os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")

import incentive_mm as imm  # noqa: E402

LEDGER = imm.RAIN_DIR_LEDGER


def main() -> int:
    if not os.path.exists(LEDGER):
        print(f"no ledger yet ({LEDGER}) — the bot writes it on the first take")
        return 0
    with open(LEDGER, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("ledger empty")
        return 0
    client = imm.build_client()
    tickers = sorted({r["ticker"] for r in rows})
    markets = {}
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        resp = client.get_markets(tickers=",".join(chunk), limit=len(chunk))
        for m in resp.get("markets") or []:
            markets[m.get("ticker", "")] = m

    print(f"{'ts':20s} {'ticker':26s} {'side':4s} {'n':>2s} {'px':>3s} "
          f"{'fair':>4s} {'edge':>5s} {'result':>7s} {'P&L$':>7s}")
    total = settled_pnl = open_mtm = 0.0
    n_bets = n_settled = n_wins = 0
    fair_sum = win_sum = 0.0
    by_day = defaultdict(float)
    for r in rows:
        n_bets += 1
        n = float(r["contracts"])
        px = float(r["price_cents"])
        fair = float(r["fair_cents"])
        side = r["take_side"]
        m = markets.get(r["ticker"], {})
        result = m.get("result") or ""
        day = r["ts"][:10]
        if result in ("yes", "no"):
            n_settled += 1
            win = (result == side)
            pnl = ((100 - px) if win else -px) / 100.0 * n
            settled_pnl += pnl
            by_day[day] += pnl
            if win:
                n_wins += 1
            # calibration sample: model prob of the TAKEN side vs outcome
            p_take = fair / 100.0 if side == "yes" else 1 - fair / 100.0
            fair_sum += p_take
            win_sum += 1.0 if win else 0.0
            status, pnl_s = result.upper(), f"{pnl:+7.2f}"
        else:
            bid = imm.market_cents(m, "yes_bid")
            ask = imm.market_cents(m, "yes_ask")
            mid = (bid + ask) / 2 if bid and ask else None
            if mid is not None:
                mark = mid if side == "yes" else 100 - mid
                mtm = (mark - px) / 100.0 * n
                open_mtm += mtm
                status, pnl_s = "OPEN", f"{mtm:+7.2f}"
            else:
                status, pnl_s = "OPEN", "      ?"
        print(f"{r['ts']:20s} {r['ticker']:26s} {side:4s} {n:2.0f} {px:3.0f} "
              f"{fair:4.0f} {float(r['edge_cents']):+5.0f} {status:>7s} {pnl_s}")
    total = settled_pnl + open_mtm
    print(f"\nbets {n_bets} | settled {n_settled} "
          f"(wins {n_wins}, hit {100 * n_wins / n_settled:.0f}%)" if n_settled
          else f"\nbets {n_bets} | none settled yet")
    if n_settled:
        print(f"model calibration: avg fair prob of taken side "
              f"{100 * fair_sum / n_settled:.0f}% vs realized {100 * win_sum / n_settled:.0f}%")
        for d in sorted(by_day):
            print(f"  {d}: {by_day[d]:+.2f}")
    print(f"settled P&L ${settled_pnl:+.2f} | open MTM ${open_mtm:+.2f} "
          f"| total ${total:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
