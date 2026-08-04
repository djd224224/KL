#!/usr/bin/env python3
r"""
imm_backfill_daily_pnl.py — rebuild per-day RAW trading P&L for the IMM bot
all the way back to its first fill, and persist it for the digest.

WHY THIS EXISTS: fills carry no client_order_id, and the bot's our_order_ids
map is pruned at 7 days, so the digest cannot attribute anything older than a
week — it showed "n/a" for earlier days. But every order the bot ever placed
was logged ("[IMM] placed <T> <SIDE> <n>x @ <p>c -> <order_id>"), so the full
id set is recoverable from run-logs/incentive-mm/incentive-mm-*.log. This
script does that once, computes each ET day's RAW P&L, and writes
run-logs/incentive-mm/daily_pnl.json for send_imm_digest.py to read.

RAW P&L per day = realized from offsetting fills + settlement on the residual
position + mark-to-market on anything still open - fees. Fills are attributed
to the ET day they occurred.

Read-only against the exchange. Safe to re-run; it rewrites the file.

Usage:  python imm_backfill_daily_pnl.py [--since 2026-07-16] [--dry]
"""
import argparse
import collections
import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import incentive_mm as imm  # noqa: E402
from incentive_mm import ET, STATUS_DIR, PnlTracker, build_client, log  # noqa: E402

OUT_PATH = os.path.join(STATUS_DIR, "daily_pnl.json")
PLACED_RE = re.compile(r"placed \S+ \w+\s+[\d.]+x @ \d+c -> ([0-9a-f-]{36})")


def recover_order_ids() -> set:
    """Every order id the bot ever logged placing, plus the live state map."""
    ids = set()
    for path in sorted(glob.glob(os.path.join(STATUS_DIR, "incentive-mm-*.log"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = PLACED_RE.search(line)
                    if m:
                        ids.add(m.group(1))
        except OSError as e:
            log(f"! log read failed {path}: {e}")
    try:
        state = json.load(open(os.path.join(STATUS_DIR, "imm_state.json"),
                               encoding="utf-8"))
        ids |= set(state.get("our_order_ids") or {})
    except (OSError, ValueError):
        pass
    return ids


def fetch_fills(client, our_ids: set, since_ts: int) -> list:
    out, cursor, seen = [], None, set()
    for _page in range(2000):
        resp = client.get_fills(min_ts=since_ts, limit=200, cursor=cursor)
        batch = resp.get("fills") or []
        for f in batch:
            fid = f.get("fill_id") or f.get("trade_id") or ""
            if f.get("order_id") in our_ids and fid not in seen:
                seen.add(fid)
                out.append(f)
        cursor = resp.get("cursor")
        if not cursor or not batch:
            break
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="2026-07-11",
                    help="ET date to start from (default: bot inception)")
    ap.add_argument("--dry", action="store_true", help="print, do not write")
    args = ap.parse_args(argv)

    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    client = build_client()

    ids = recover_order_ids()
    log(f"recovered {len(ids):,} unique bot order ids from logs + state")
    fills = fetch_fills(client, ids, int(since.timestamp()))
    log(f"{len(fills):,} bot-attributed fills since {args.since}")
    if not fills:
        log("nothing to do")
        return 0

    tickers = sorted({f["ticker"] for f in fills})
    mk = {}
    for i in range(0, len(tickers), 50):
        chunk = tickers[i:i + 50]
        try:
            resp = client.get_markets(tickers=",".join(chunk), limit=len(chunk))
            for m in (resp.get("markets") or []):
                mk[m.get("ticker")] = m
        except Exception as e:
            log(f"! market read failed: {e}")
    log(f"{len(mk):,} markets resolved")

    by_day = collections.defaultdict(list)
    for f in fills:
        ts = f.get("ts")
        if not ts:
            continue
        day = datetime.fromtimestamp(float(ts), timezone.utc).astimezone(ET).date()
        by_day[day].append(f)

    out = {}
    for day in sorted(by_day):
        pnl = PnlTracker()
        fees = 0.0
        for f in sorted(by_day[day], key=lambda x: x.get("ts") or 0):
            cnt = float(f.get("count_fp") or f.get("count") or 0)
            pxc = float(f.get("yes_price_dollars") or 0) * 100
            side, action = f.get("side"), f.get("action")
            if side in ("yes", "no") and action in ("buy", "sell") and cnt > 0:
                pnl.on_fill(f.get("ticker", "?"), side, action, cnt, pxc)
            fees += float(f.get("fee_cost") or 0)
        settle = mtm = 0.0
        for t, p in pnl.pos.items():
            if abs(p) < 0.01:
                continue
            a = pnl.avg.get(t, 0.0)
            m = mk.get(t, {})
            res = str(m.get("result") or "").lower()
            if res in ("yes", "no"):
                settle += p * ((100.0 if res == "yes" else 0.0) - a) / 100.0
            else:
                bid = float(m.get("yes_bid_dollars") or 0) * 100
                ask = float(m.get("yes_ask_dollars") or 0) * 100
                if bid and ask:
                    mtm += p * ((bid + ask) / 2 - a) / 100.0
        raw = sum(pnl.realized.values()) + settle + mtm - fees
        out[day.isoformat()] = {
            "raw": round(raw, 2),
            "realized": round(sum(pnl.realized.values()), 2),
            "settle": round(settle, 2),
            "mtm": round(mtm, 2),
            "fees": round(fees, 2),
            "contracts": round(sum(float(f.get("count_fp") or f.get("count") or 0)
                                   for f in by_day[day]), 0),
            "fills": len(by_day[day]),
        }

    print(f"\n{'DATE':12s} {'RAW$':>11s} {'realized':>10s} {'settle':>10s} "
          f"{'mtm':>9s} {'contracts':>10s}")
    tot = 0.0
    for d in sorted(out):
        r = out[d]
        tot += r["raw"]
        print(f"{d:12s} {r['raw']:>+11,.2f} {r['realized']:>+10,.2f} "
              f"{r['settle']:>+10,.2f} {r['mtm']:>+9,.2f} {r['contracts']:>10,.0f}")
    print(f"{'TOTAL':12s} {tot:>+11,.2f}")

    if not args.dry:
        os.makedirs(STATUS_DIR, exist_ok=True)
        tmp = OUT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1, sort_keys=True)
        os.replace(tmp, OUT_PATH)
        log(f"wrote {OUT_PATH} ({len(out)} days)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
