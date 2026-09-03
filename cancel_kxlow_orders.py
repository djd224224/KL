# -*- coding: utf-8 -*-
"""Cancel every resting KXLOW order on the account. Exchange-side pause
enforcement for the low-temp bot: the repo-flag pause (low_temp.paused)
depends on the laptop syncing main, which failed silently (bot still
quoting 2026-09-02) — this sweeps the orders at the exchange instead.

Runs from .github/workflows/kxlow_pause_enforcer.yml every 30 min while
low_temp.paused exists on main. Touches ONLY tickers starting with KXLOW —
never any other bot's orders. Auth mirrors fetch_settlements_csv.py.

Output stays to counts (public Actions logs).
"""

import os
import sys

from fetch_settlements_csv import load_private_key
from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient

CANCELABLE = ("resting", "partial_filled", "partially_filled", "pending")


def main():
    key_id = os.environ.get("KALSHI_API_KEY_ID", "c3204983-77fc-491b-99f7-136600698178")
    private_key = load_private_key(
        b64_key=os.environ.get("KALSHI_PRIVATE_KEY", ""),
        file_path=os.environ.get("KALSHI_PRIVATE_KEY_PATH", "Lisa_Kalshi.txt"),
    )
    client = ExchangeClient(
        exchange_api_base="https://api.elections.kalshi.com/trade-api/v2",
        key_id=key_id,
        private_key=private_key,
    )

    orders, cursor = [], None
    for _ in range(50):
        resp = client.get_orders(status="resting", limit=200, cursor=cursor)
        if not isinstance(resp, dict):
            print("ERROR: unexpected get_orders response type")
            return 1
        orders.extend(resp.get("orders") or [])
        cursor = resp.get("cursor") or None
        if not cursor:
            break

    targets = [
        o for o in orders
        if str(o.get("ticker", "")).startswith("KXLOW")
        and (o.get("status") in CANCELABLE or (o.get("remaining_count") or 0) > 0)
    ]
    print(f"resting orders total: {len(orders)}; KXLOW targets: {len(targets)}")

    cancelled, failed = 0, 0
    by_series = {}
    for o in targets:
        series = str(o.get("ticker", ""))[:12].split("-")[0]
        try:
            client.cancel_order(order_id=o["order_id"])
            cancelled += 1
            by_series[series] = by_series.get(series, 0) + 1
        except Exception as e:
            failed += 1
            print(f"cancel failed ({series}): {type(e).__name__}")

    print(f"cancelled {cancelled} KXLOW orders, {failed} failures")
    if by_series:
        for s, n in sorted(by_series.items()):
            print(f"  {s}: {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
