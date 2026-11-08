#!/usr/bin/env python3
"""CHEAP-SIDE BOOK scanner + list writer (Jack 2026-11-08: "scan incentive
rewards markets, with far out settle dates (6M+) where i can place 1-2c quotes
with little fill risk but can earn $$", "Build it", then on the scan: "Truly
far: Dem Senate seats 2029, 51st state, Supreme Court makeup 2029, French
presidency 2027 ^these all fit. the others dont. i want truly far, 6M+").

For every market of the named series with an active LIQUIDITY program and a
horizon (the earlier of Kalshi's expected_expiration_time and close_time) at
least --min-days out, it reads the book (the bot's own resting orders netted
out) and scores a 1c and a 2c rung on each side -- a YES bid, and a NO bid (a
YES ask at 98-99c) -- at sizes up to --max-usd of risk, with the bot's own
cheap_side_quote (whole-cent walk, never crossing, the opposite side reaching
target alone). Per market it takes the best $/day, then the smallest risk
holding 95% of it, and keeps it when the rung projects at least --bar over the
rest of the program period. A market the bot already earns more on under its
family rules is left to them (--include-quoted lists it anyway).

Kalshi's dates are NOT when the answer lands (Grammy nominations read 358 days
out, announced 11/16): --series is the hand-picked truly-far set, and the
horizon check is only a backstop.

    python cheap_scan.py --series KXDSENATESEATS,KXSTATE51,KXSCOTUSPOWER,KXFRENCHPRES
    python cheap_scan.py --series ... --max-usd 200 --report out.json
    python cheap_scan.py --series ... --write     # writes run-logs/incentive-mm/cheap_side_books.json

--write replaces the named series' entries (others in the file stay); the
bot hot-loads the file at its next universe refresh (10 min). Each entry stops
at its program's end; a later program needs a re-run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import incentive_mm as imm                      # noqa: E402  (reads only, but --write)
import kalshi_reads                              # noqa: E402
import send_imm_new_programs as snp             # noqa: E402

SIZES = (1000, 1500, 2000, 2500, 3000, 4000, 5000, 7500, 10000)


def ts(s) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def zs(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def rd(path, params=None):
    for i in range(3):
        try:
            return kalshi_reads.read(path, params or {})
        except Exception as ex:  # noqa: BLE001
            err = ex
            time.sleep(2 + 2 * i)
    print(f"! read failed {path}: {err}", file=sys.stderr)
    return None


def programs(client, prefixes, now):
    per = {}
    for p in snp.fetch_programs_raw(client):
        t = p.get("market_ticker") or ""
        if p.get("incentive_type") != "liquidity" or not p.get("end_date") or not p.get("start_date"):
            continue
        if ts(p["end_date"]) <= now or not t.split("-")[0].startswith(prefixes):
            continue
        per.setdefault(t, []).append(p)
    out = {}
    for t, ps in per.items():
        end = max(ts(p["end_date"]) for p in ps)
        cur = [p for p in ps if ts(p["end_date"]) == end]
        st = min(ts(p["start_date"]) for p in cur)
        days = max((end - st).total_seconds() / 86400, 1 / 24)
        out[t] = {"pd": sum(float(p.get("period_reward") or 0) / 10000 for p in cur) / days,
                  "start": st, "end": end, "target": float(cur[0].get("target_size_fp") or 1000),
                  "df": (cur[0].get("discount_factor_bps") or 5000) / 10000}
    return out


def own_orders(client):
    own, exact, cursor = {}, {}, None
    for _ in range(60):
        q = {"status": "resting", "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        r = client.get("/portfolio/orders", params=q) or {}
        for o in r.get("orders") or []:
            if not str(o.get("client_order_id", "")).startswith(imm.CLIENT_ORDER_PREFIX + "-"):
                continue
            bp = imm.order_yes_book_cents(o)
            if bp:
                own.setdefault(o["ticker"], []).append((bp[0], bp[1], imm.order_remaining(o)))
                ox = imm.order_yes_exact_cents(o)
                exact.setdefault(o["ticker"], []).append(
                    (bp[0], float(ox if ox is not None else bp[1]), imm.order_remaining(o)))
        cursor = r.get("cursor")
        if not cursor or not r.get("orders"):
            break
    return own, exact


def best_rung(t, pr, ob, own, own_exact, max_usd, hrs):
    y0, n0 = imm.orderbook_levels(ob)
    ye, ne = imm.orderbook_exact_levels(ob)
    seen, cands = set(), []
    for side in ("yes", "no"):
        for px in (1, 2):
            for n in SIZES:
                if n * px / 100.0 > max_usd + 1e-9:
                    continue
                e = {"size": n, "cheap": {"side": side, "px": px}}
                q, info = imm.cheap_side_quote(t, e, y0, n0, own, ye, ne, own_exact,
                                               pr["target"], pr["df"], 0.0)
                if q is None:
                    continue
                key = (side, info["px"], q.count)
                if key in seen:
                    continue
                seen.add(key)
                usd = float(info["share"]) / 2.0 * pr["pd"]
                cands.append({"side": side, "px": info["px"], "size": q.count, "share": info["share"],
                              "usd_day": usd, "risk": q.count * info["px"] / 100.0,
                              "proj": usd * hrs / 24.0, "limit": info.get("limit")})
    if not cands:
        return None, None
    top = max(c["usd_day"] for c in cands)
    pick = sorted((c for c in cands if c["usd_day"] >= 0.95 * top),
                  key=lambda c: (c["risk"], -c["usd_day"]))[0]
    # what the bot earns there now, on its own resting orders (live book)
    cur_frac, _ = imm.estimate_reward_share(y0, n0, own, pr["target"], pr["df"], own_in_book=True)
    return pick, cur_frac * pr["pd"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", required=True, help="comma-separated series PREFIXES (the truly-far set)")
    ap.add_argument("--min-days", type=float, default=182.0, help="horizon backstop (6M)")
    ap.add_argument("--max-usd", type=float, default=50.0, help="size x px a market, at most")
    ap.add_argument("--bar", type=float, default=1.10, help="$ the rung must project by its program end")
    ap.add_argument("--include-quoted", action="store_true",
                    help="list a market even where the bot earns more under its family rules")
    ap.add_argument("--report", help="write every scored market here (.json)")
    ap.add_argument("--write", action="store_true", help="write the list (the bot hot-loads it)")
    a = ap.parse_args()
    prefixes = tuple(s.strip() for s in a.series.split(",") if s.strip())
    now = datetime.now(timezone.utc)
    client = imm._fair_reader_client()
    progs = programs(client, prefixes, now)
    tick = sorted(progs)
    mk = {}
    for i in range(0, len(tick), 100):
        j = rd("/markets", {"tickers": ",".join(tick[i:i + 100]), "limit": 1000}) or {}
        for m in j.get("markets") or []:
            mk[m["ticker"]] = m
    far = {}
    for t in tick:
        m = mk.get(t) or {}
        if m.get("status") not in ("active", "open") or imm.IncentiveMarketMaker._blocked(t):
            continue
        ds = [ts(m[k]) for k in ("expected_expiration_time", "close_time") if m.get(k)]
        if ds and (min(ds) - now).total_seconds() / 86400 >= a.min_days:
            far[t] = (m, min(ds))
    own, own_exact = own_orders(client)

    def book(t):
        ob = rd(f"/markets/{t}/orderbook")
        time.sleep(0.2)
        return t, ob
    with ThreadPoolExecutor(4) as ex:
        books = dict(ex.map(book, sorted(far)))

    rows, why = [], {"no book": 0, "no paying 1-2c rung": 0, "under bar": 0, "bot earns more": 0}
    for t, (m, hz) in sorted(far.items()):
        ob = books.get(t)
        if not ob:
            why["no book"] += 1
            continue
        pr = progs[t]
        hrs = (pr["end"] - max(now, pr["start"])).total_seconds() / 3600
        pick, cur = best_rung(t, pr, ob, own.get(t, []), own_exact.get(t, []), a.max_usd, hrs)
        r = {"ticker": t, "event": m.get("event_ticker"), "sub": (m.get("yes_sub_title") or "")[:40],
             "horizon": zs(hz), "stop": zs(pr["end"]), "hrs_left": round(hrs, 1),
             "pool_day": round(pr["pd"], 2), "bot_usd_day_now": round(cur or 0.0, 2),
             "bot_resting": sum(q for _s, _p, q in own.get(t, [])), "pick": pick}
        if pick is None:
            why["no paying 1-2c rung"] += 1
            r["verdict"] = "none"
        elif pick["proj"] < a.bar:
            why["under bar"] += 1
            r["verdict"] = "under bar"
        elif (cur or 0.0) >= pick["usd_day"] and not a.include_quoted:
            why["bot earns more"] += 1
            r["verdict"] = "bot earns more"
        else:
            r["verdict"] = "LIST"
        rows.append(r)
    take = [r for r in rows if r["verdict"] == "LIST"]
    print(f"cheap_scan {zs(now)}: {len(progs)} program markets in {','.join(prefixes)}; "
          f"{len(far)} at {a.min_days:.0f}d+; listing {len(take)}; skipped {why}")
    print(f"{'ticker':36} {'sub':24} {'side':4} {'px':>2} {'size':>6} {'share':>6} {'$/day':>6} "
          f"{'risk$':>6} {'proj$':>6} {'now$/d':>6} stop")
    for r in sorted(rows, key=lambda r: -((r["pick"] or {}).get("usd_day") or 0)):
        p = r["pick"] or {}
        if not p:
            continue
        print(f"{r['ticker'][:36]:36} {r['sub'][:24]:24} {p['side'].upper():4} {p['px']:2d} {p['size']:6d} "
              f"{p['share'] * 100:5.0f}% {p['usd_day']:6.2f} {p['risk']:6.0f} {p['proj']:6.2f} "
              f"{r['bot_usd_day_now']:6.2f} {r['stop'][5:16]}  {'' if r['verdict'] == 'LIST' else r['verdict']}")
    print(f"TOTAL listed: ${sum(r['pick']['usd_day'] for r in take):,.2f}/day modelled, "
          f"${sum(r['pick']['risk'] for r in take):,.0f} if every rung fills, "
          f"${sum(r['pick']['proj'] for r in take):,.0f} to the program ends")
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump({"at": zs(now), "args": vars(a), "skipped": why, "rows": rows}, fh, indent=1)
    if a.write:
        path = imm.cheap_path()
        try:
            with open(path, encoding="utf-8") as fh:
                cur = json.load(fh) or {}
        except (OSError, ValueError):
            cur = {}
        L = {t: e for t, e in (cur.get("markets") or {}).items()
             if not t.split("-")[0].startswith(prefixes)}
        stamp = now.strftime("%Y-%m-%dT%H:%MZ")
        for r in take:
            p = r["pick"]
            L[r["ticker"]] = {"side": p["side"], "px": p["px"], "size": p["size"], "stop": r["stop"],
                              "event": r["event"], "pool_day": r["pool_day"],
                              "est_usd_day": round(p["usd_day"], 2), "proj_period": round(p["proj"], 2),
                              "risk_usd": round(p["risk"], 2), "horizon": r["horizon"],
                              "added": f"cheap_scan {stamp} (share {p['share'] * 100:.0f}%)"}
        cur["markets"] = L
        cur["last_write"] = f"cheap_scan.py {stamp}: {len(take)} for {','.join(prefixes)}, max ${a.max_usd:g}"
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
        print(f"WROTE {len(take)} -> {path} ({len(L)} listed in all)")
    else:
        print("dry run: nothing written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
