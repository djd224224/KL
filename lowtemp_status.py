#!/usr/bin/env python3
"""KXLOW low-temp bot status probe (runs in GitHub Actions where Kalshi is
reachable). The bot itself runs on the local Task Scheduler, so run health
lives in run-logs\\low-temp on the box — but outcomes live on the exchange:
this prints current KXLOW activity and settled P&L since the bot went live
(2026-07-22). Read-only GETs; aggregates only (public repo, public logs —
no order-level detail, no balances).

Field semantics follow fetch_settlements_csv.py / analyze_kalshi_dashboard:
settlement `revenue` (cents) is NET profit per market (the dashboard
reconstructs payout as Profit + cost).
"""

from collections import defaultdict
from datetime import datetime, timezone

import incentive_mm as imm

LIVE_SINCE = "2026-07-21"          # bot live 2026-07-22; one day of margin
PAGE_CAP = 80                      # 80 x 200 = 16k settlements max swept


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def main() -> None:
    now = datetime.now(timezone.utc)
    print(f"UTC now: {now:%Y-%m-%d %H:%M:%S} (ET {now.astimezone(imm.ET):%H:%M})")
    client = imm.build_client()

    section("1. CURRENT KXLOW ACTIVITY")
    resting = []
    cursor = None
    while True:
        resp = client.get_orders(status="resting", limit=200, cursor=cursor)
        batch = resp.get("orders") or []
        resting.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch or len(resting) > 5000:
            break
    low_orders = [o for o in resting
                  if str(o.get("ticker", "")).startswith("KXLOW")]
    cities = sorted({str(o.get("ticker", "")).split("-")[0][5:]
                     for o in low_orders})
    newest = max((str(o.get("created_time") or "") for o in low_orders),
                 default="")
    print(f"resting KXLOW orders: {len(low_orders)}"
          f" across cities {cities or '[]'}; newest created {newest or 'n/a'}")

    pos = []
    cursor = None
    while True:
        resp = client.get_positions(limit=200, cursor=cursor)
        batch = resp.get("market_positions") or []
        pos.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch or len(pos) > 5000:
            break
    low_pos = [p for p in pos if str(p.get("ticker", "")).startswith("KXLOW")
               and abs(float(p.get("position") or 0)) > 0]
    print(f"open KXLOW positions (unsettled): {len(low_pos)} markets, "
          f"{sum(abs(float(p.get('position') or 0)) for p in low_pos):.0f} contracts")

    section(f"2. SETTLED KXLOW P&L since {LIVE_SINCE}")
    setts = []
    cursor = None
    pages = 0
    stop = False
    while not stop and pages < PAGE_CAP:
        resp = client.get_portfolio_settlements(limit=200, cursor=cursor)
        batch = resp.get("settlements") or []
        pages += 1
        for s in batch:
            ts = str(s.get("settled_time") or "")
            if ts and ts[:10] < LIVE_SINCE:
                stop = True
                break
            setts.append(s)
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
    low = [s for s in setts if str(s.get("ticker") or s.get("market_ticker")
                                   or "").startswith("KXLOW")]
    print(f"(swept {len(setts)} settlements over {pages} pages; "
          f"KXLOW: {len(low)})")
    if not low:
        print("no KXLOW settlements in range")
        return

    def pnl(s) -> float:
        return (s.get("revenue") or 0) / 100.0

    total = sum(pnl(s) for s in low)
    wins = sum(1 for s in low if pnl(s) > 0)
    losses = sum(1 for s in low if pnl(s) < 0)
    flats = len(low) - wins - losses
    print(f"TOTAL net P&L: ${total:+,.2f} over {len(low)} settled markets "
          f"({wins}W/{losses}L/{flats} flat, "
          f"{100.0 * wins / max(1, wins + losses):.0f}% win rate)")

    by_week: dict = defaultdict(float)
    by_day: dict = defaultdict(float)
    by_city: dict = defaultdict(lambda: [0.0, 0])
    for s in low:
        d = str(s.get("settled_time") or "")[:10]
        by_day[d] += pnl(s)
        try:
            iso = datetime.strptime(d, "%Y-%m-%d").isocalendar()
            by_week[f"{iso[0]}-W{iso[1]:02d}"] += pnl(s)
        except ValueError:
            pass
        city = str(s.get("ticker") or "").split("-")[0][5:]
        by_city[city][0] += pnl(s)
        by_city[city][1] += 1

    print("\nby week:")
    for wk in sorted(by_week):
        print(f"  {wk}: ${by_week[wk]:+,.2f}")
    print("\nlast 10 settlement days:")
    for d in sorted(by_day)[-10:]:
        print(f"  {d}: ${by_day[d]:+,.2f}")
    print("\nby city:")
    for city, (p, n) in sorted(by_city.items(), key=lambda kv: kv[1][0]):
        print(f"  {city:8s} ${p:+9,.2f}  ({n} markets)")


if __name__ == "__main__":
    main()
