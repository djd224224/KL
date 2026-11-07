"""Re-apply a wave's hand tuning to its post_vote_dead_books.json entries.

pvd_scan.py rebuilds a relit entry from scratch, so anything set by hand on the
previous period (widened limits, a price band) is lost at every relight. This
re-applies it to the wave's LIVE entries (stop in the future, period_start at or
after --since, series in --series), idempotently:

  --room N           widen the no-last-trade limits N c each way (bid_max += N,
                     ask_min -= N, never crossing), once per entry ("widened" note)
  --price-min/-max   the entry's price band (the bot's default is 3-97c)
  --bank-size/--base-size/--bank-usd/--bank-hist
                     BANK EARLY: an entry rests at --bank-size a side until the
                     dashboard (imm_pvd_watch history.json "pcum", banked est THIS
                     program period) shows --bank-usd banked, then at --base-size.
                     A relit entry starts a new period, so it goes back up. A side
                     the scanner sized above --bank-size (a decided side) is kept.
                     If history.json is older than --bank-stale-min, sizes are left
                     as they are.

Holds pvd_scan's lock while it reads and writes, so a scan can't overwrite it
(and it waits up to --wait secs for a running scan, then gives up: the next run
catches up). Dry run unless --write.

2028 election wave, Jack 2026-11-06: "i said prioritize quoting" (3c room, after
other makers stepped inside the scan-time limits), "also do 2 and 3" (2-98c
band on the long shots and safe seats, a relight task for the wave) and
"200 a side until $1.30 is banked, then back down" (bank early: Kalshi pays a
period only once it accrues $1, and 816 of 1,722 projected just $1.10-1.50).
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
LIST = os.path.join(HERE, "run-logs", "incentive-mm", "post_vote_dead_books.json")
LOCK = os.path.join(HERE, "run-logs", "pvd-scan", "scan.lock")


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", required=True, help="comma-separated EXACT series names")
    ap.add_argument("--since", required=True, help="ISO: only entries whose period_start is at or after this")
    ap.add_argument("--room", type=int, default=0)
    ap.add_argument("--price-min", type=int)
    ap.add_argument("--price-max", type=int)
    ap.add_argument("--bank-size", type=int)
    ap.add_argument("--base-size", type=int)
    ap.add_argument("--bank-usd", type=float, default=1.30)
    ap.add_argument("--bank-hist", help="imm_pvd_watch history.json of the wave's dashboard")
    ap.add_argument("--bank-stale-min", type=float, default=30.0)
    ap.add_argument("--note", default="")
    ap.add_argument("--wait", type=int, default=120)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    series = {s.strip() for s in a.series.split(",") if s.strip()}
    since = ts(a.since)
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if a.write:
        t0 = time.time()
        while os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) < 1500:
            if time.time() - t0 > a.wait:
                print(f"{stamp} scan.lock held for {a.wait}s -- not writing (the next run catches up)")
                return 2
            time.sleep(3)
        open(LOCK, "w").close()
    bank = None                     # {ticker: [period_start, banked $]} or None = sizes untouched
    bank_msg = ""
    if a.bank_size and a.base_size and a.bank_hist:
        try:
            age_min = (time.time() - os.path.getmtime(a.bank_hist)) / 60.0
            if age_min > a.bank_stale_min:
                bank_msg = f"; bank sizing SKIPPED: {a.bank_hist} is {age_min:.0f} min old"
            else:
                bank = json.load(open(a.bank_hist, encoding="utf-8")).get("pcum") or {}
        except (OSError, ValueError) as ex:
            bank_msg = f"; bank sizing SKIPPED: {ex}"
    try:
        cur = json.load(open(LIST, encoding="utf-8"))
        n_room = n_band = n_seen = n_up = n_down = n_banked = 0
        for t, e in (cur.get("markets") or {}).items():
            if t.split("-")[0] not in series:
                continue
            try:
                if ts(e["stop"]) <= now or ts(e.get("period_start") or "1970-01-01T00:00:00Z") < since:
                    continue
            except (KeyError, ValueError):
                continue
            n_seen += 1
            if a.room > 0 and "widened" not in e:
                b0, a0 = int(e["bid_max"]), int(e["ask_min"])
                r = max(0, min(a.room, (a0 - b0 - 1) // 2))     # never let the bid limit reach the ask limit
                e["bid_max"], e["ask_min"] = b0 + r, a0 - r
                e["widened"] = f"{stamp}: limits {b0}/{a0} -> {b0 + r}/{a0 - r} ({a.room}c room each way) {a.note}".strip()
                n_room += 1
            if a.price_min is not None and a.price_max is not None and (
                    e.get("price_min") != a.price_min or e.get("price_max") != a.price_max):
                e["price_min"], e["price_max"] = a.price_min, a.price_max
                e["band"] = f"{stamp}: {a.price_min}-{a.price_max}c {a.note}".strip()
                n_band += 1
            if bank is not None:
                pk, pv = bank.get(t) or [None, 0.0]
                banked = float(pv) if pk == e.get("period_start") else 0.0
                done = banked >= a.bank_usd
                n_banked += done
                want = a.base_size if done else a.bank_size
                old = int(e.get("size") or 0)
                # a side the scanner sized above the bank size (a decided side at 300) keeps its own size
                sides = {k: e[k] for k in ("bid_size", "ask_size") if k in e and int(e[k]) > a.bank_size}
                if old != want or any(k in e and k not in sides for k in ("bid_size", "ask_size")):
                    for k in ("bid_size", "ask_size"):
                        e.pop(k, None)
                    e.update(sides)
                    e["size"] = want
                    e["bank"] = (f"{stamp}: banked ${banked:.2f} this period -> {want} a side "
                                 f"({'past' if done else 'under'} ${a.bank_usd:.2f}) {a.note}").strip()
                    if want > old:
                        n_up += 1
                    else:
                        n_down += 1
        msg = (f"{stamp} {len(series)} series since {a.since}: {n_seen} live entries, "
               f"{n_room} widened {a.room}c, {n_band} banded "
               f"{a.price_min}-{a.price_max}c" if a.price_min is not None else
               f"{stamp} {len(series)} series since {a.since}: {n_seen} live entries, {n_room} widened {a.room}c")
        if bank is not None:
            msg += (f"; bank early: {n_banked} past ${a.bank_usd:.2f} this period, "
                    f"{n_up} up to {a.bank_size}, {n_down} down to {a.base_size}")
        msg += bank_msg
        if a.write and (n_room or n_band or n_up or n_down):
            cur[f"tune_{stamp}"] = msg
            tmp = LIST + ".tune.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cur, fh, indent=1, sort_keys=True)
            os.replace(tmp, LIST)
            print("WROTE", msg)
        else:
            print(("nothing to change: " if a.write else "dry run: ") + msg)
    finally:
        if a.write and os.path.exists(LOCK):
            os.remove(LOCK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
