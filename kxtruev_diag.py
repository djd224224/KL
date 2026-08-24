#!/usr/bin/env python3
"""One-shot KXTRUEV diagnostic (runs in GitHub Actions, where Kalshi is
reachable; Claude's cloud container cannot reach kalshi.com).

Ground truth for "KXTRUEV still not quoting", from the exchange itself:
  1. MARKETS  — do KXTRUEV markets exist / are any open right now, with
     what open_time/close_time? (The close-anchored cutoff needs an open
     market with a close_time; print-day-only listing should show
     same-day opens.)
  2. PROGRAMS — does ANY active liquidity incentive program cover a
     KXTRUEV market? Selection is program-driven: with no program the bot
     never quotes the series regardless of the allowlist. Uses the bot's
     own fetch_programs() so the view is exactly what the bot sees, plus
     a raw-feed sweep to catch entries fetch_programs filters out
     (wrong type / window / paid_out).
  3. VERDICT  — one line naming the blocker, if any.

READ-ONLY: GETs only, live=False, no orders; the workflow points
IMM_STATUS_DIR at a scratch dir so no state is read or written anywhere
that matters. Prints only public exchange data (this repo is public and
so are its Action logs — keep account data out of here).
"""

from datetime import datetime, timezone

import requests

import incentive_mm as imm

BASE = imm.KALSHI_API_BASE


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print(title)
    print("=" * 64)


def public_get(path: str, **params):
    r = requests.get(BASE + path, params=params or None, timeout=20)
    r.raise_for_status()
    return r.json()


def main() -> None:
    now = datetime.now(timezone.utc)
    print(f"UTC now: {now:%Y-%m-%d %H:%M:%S}"
          f" (ET {now.astimezone(imm.ET):%H:%M})")

    section("1. KXTRUEV MARKETS (public /markets, all statuses)")
    markets = []
    cursor = None
    while True:
        params = {"series_ticker": "KXTRUEV", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        resp = public_get("/markets", **params)
        batch = resp.get("markets") or []
        markets.extend(batch)
        cursor = resp.get("cursor")
        if not cursor or not batch or len(markets) > 2000:
            break
    print(f"total KXTRUEV markets returned: {len(markets)}")
    open_now = [m for m in markets if m.get("status") in ("active", "open")]
    for m in sorted(markets, key=lambda m: m.get("close_time") or "")[-40:]:
        print(f"  {m.get('ticker', '?'):42s} {m.get('status', '?'):10s} "
              f"open {m.get('open_time', '?')}  close {m.get('close_time', '?')}")
    print(f"OPEN right now: {len(open_now)}")

    section("2. INCENTIVE PROGRAMS (the bot's own fetch_programs view)")
    client = imm.build_client()
    bot = imm.IncentiveMarketMaker(client=client, live=False)
    by_market = bot.fetch_programs()
    print(f"active liquidity programs cover {len(by_market)} markets total")
    truev = {t: v for t, v in by_market.items()
             if t.split("-")[0] == "KXTRUEV"}
    if truev:
        for t, v in sorted(truev.items()):
            print(f"  {t:42s} ${v['dollars_per_day']:.2f}/day  "
                  f"target {v['target']:.0f}  ends {v['end']:%Y-%m-%d %H:%M}Z")
    else:
        print("  NO active liquidity program covers any KXTRUEV market")
        for t in sorted(by_market)[:10]:
            print(f"    (feed sample) {t}")

    # Raw sweep: an entry failing fetch_programs' filters (type != liquidity,
    # outside its window, paid_out) is invisible above but visible here.
    raw = []
    cursor = None
    for _page in range(20):
        params = {"limit": 1000, "status": "active"}
        if cursor:
            params["cursor"] = cursor
        resp = client.get("/incentive_programs", params=params)
        batch = resp.get("incentive_programs") or []
        raw.extend(batch)
        cursor = resp.get("next_cursor")
        if not cursor or not batch:
            break
    raw_truev = [p for p in raw
                 if (p.get("market_ticker") or "").startswith("KXTRUEV")]
    print(f"raw active-feed entries: {len(raw)}; on KXTRUEV: {len(raw_truev)}")
    for p in raw_truev[:20]:
        print(f"  raw: {p}")

    section("3. VERDICT")
    if not markets:
        print("KXTRUEV: no markets exist at all -> nothing to quote "
              "(series dark on Kalshi).")
    elif not open_now:
        print("KXTRUEV: markets exist but none is open at this instant -> "
              "nothing quotable right now; re-run during the print day.")
    if truev:
        print("Program EXISTS -> a still-silent bot is a bot-side problem "
              "(deploy/restart chain, floors, or screens); next stop is the "
              "cycle log on the trading box.")
    elif raw_truev:
        print("KXTRUEV program entries exist in the raw feed but fail "
              "fetch_programs' filters (type/window/paid_out) -> the bot "
              "correctly sees $0 for them; details above.")
    else:
        print("NO incentive program on KXTRUEV -> the IMM bot will NEVER "
              "quote it, by design (selection is program-driven). The "
              "allowlist/cutoff work is correct but idle until Kalshi "
              "lights a program on the series.")


if __name__ == "__main__":
    main()
