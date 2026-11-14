#!/usr/bin/env python3
"""WHERE DOES THE NEXT QUOTE DOLLAR EARN MOST? (the market-wave playbook's
what-if; Jack 2026-11-07: "where should i put the incremental quote dollar?").

For every market the bot rests orders in: the latest cycle-log row (own size and
price per side, pool, target, discount), the live order book (public reads, 6
threads, ~12 min for ~3,000 markets), and the bot's own estimate_reward_share
with ONE side +--bump (default +50%) at its price -- the book level and our own
size both, own_in_book=True. Marginal $/day = pool x d(share); marginal $ at
risk = the extra contracts x what each ties up (a bid its price, an ask 100 -
its price). Read-only: nothing of the bot's is written (IMM_STATUS_DIR is a temp
dir).

Groups: each registry wave (pvd_waves.py), the legacy 2028 / political waves
(from run_pvd_scan_waves.cmd), other listed markets, other bot markets. Per group
by cost band, then wave series x band, then the top sides.

On 11/07 it matched the bot's est_frac on 91-98% of markets; raises realized
~55-75% of its prediction (others react, and a bare <=2c raise showed ~0 at
first). Treat it as an upper bound.

    python pvd_marginal.py [--bump 0.5] [--json out.json]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import statistics
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["IMM_STATUS_DIR"] = tempfile.mkdtemp(prefix="pvd_marginal_")   # nothing of the bot's is written
sys.path.insert(0, HERE)
import incentive_mm as imm                                   # noqa: E402
from kalshi_reads import kalshi_get                          # noqa: E402

LOG_DIR = os.path.join(HERE, "run-logs", "incentive-mm")
LIST = os.path.join(LOG_DIR, "post_vote_dead_books.json")
REGISTRY = os.path.join(HERE, "run-logs", "pvd-scan", "waves.json")
TASK = os.path.join(HERE, "run_pvd_scan_waves.cmd")
TAIL = 90 * 1024 * 1024
BANDS = [(2, "2c or under"), (5, "2-5c"), (15, "5-15c"), (30, "15-30c"), (50, "30-50c"), (70, "50-70c"),
         (90, "70-90c"), (100, "over 90c")]


def band(c: float) -> str:
    for hi, lbl in BANDS:
        if c <= hi:
            return lbl
    return "over 90c"


def wave_of() -> dict:
    """series -> group name: registry waves, then the legacy waves in the task file."""
    out = {}
    try:
        for name, w in (json.load(open(REGISTRY, encoding="utf-8")).get("waves") or {}).items():
            for s in (w.get("series") or []) + (w.get("near") or []):
                out[s] = name
    except (OSError, ValueError):
        pass
    try:
        for line in open(TASK, encoding="utf-8"):
            for var, lbl in (("set W28=", "2028 wave"), ("set POL=", "political wave")):
                if line.startswith(var):
                    for s in line.split("=", 1)[1].strip().split(","):
                        out.setdefault(s.strip(), lbl)
    except OSError:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bump", type=float, default=0.5)
    ap.add_argument("--json", default=os.path.join(HERE, "run-logs", "pvd-scan", "marginal_last.json"))
    a = ap.parse_args()
    now = datetime.now(timezone.utc)
    pvd = (json.load(open(LIST, encoding="utf-8")).get("markets") or {}) if os.path.exists(LIST) else {}
    waves = wave_of()

    def group(t: str) -> str:
        e, s = pvd.get(t), t.split("-")[0]
        live = bool(e) and datetime.fromisoformat(str(e.get("stop", "1970-01-01T00:00:00Z")).replace("Z", "+00:00")) > now
        if live:
            return waves.get(s, "other listed")
        return "other bot markets"

    logf = os.path.join(LOG_DIR, f"cycle_log_{now:%Y-%m-%d}.csv")
    with open(logf, "rb") as fh:
        hdr = fh.readline().decode().strip().split(",")
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - TAIL))
        chunk = fh.read().decode("utf-8", "replace")
    ix = {k: i for i, k in enumerate(hdr)}
    last = {}
    for line in chunk.split("\n")[1:]:
        p = line.split(",")
        if len(p) >= len(hdr):
            last[p[ix["ticker"]]] = p
    tmax = max(p[ix["ts"]] for p in last.values())
    cut = datetime.fromisoformat(tmax.replace("Z", "+00:00")).timestamp() - 900
    rows = {}
    for t, p in last.items():
        if datetime.fromisoformat(p[ix["ts"]].replace("Z", "+00:00")).timestamp() < cut:
            continue
        f = lambda k: float(p[ix[k]] or 0)    # noqa: E731
        ob, oa = f("own_bid_ct"), f("own_ask_ct")
        if ob <= 0 and oa <= 0:
            continue
        rows[t] = dict(pool=f("pool_per_day"), target=f("target"), df=f("discount") or 0.5, est=f("est_frac"),
                       ob=ob, oa=oa, bt=int(f("own_bid_top")), at=int(f("own_ask_top")))
    print(f"{len(rows)} markets with our orders in the cycle log ({tmax})", flush=True)

    def book(t):
        for i in range(4):
            try:
                return t, kalshi_get(f"/markets/{t}/orderbook", prefer="public")
            except Exception:  # noqa: BLE001
                time.sleep(1.5 * (i + 1))
        return t, None

    t0 = time.time()
    books = {}
    with ThreadPoolExecutor(6) as ex:
        for t, ob in ex.map(book, list(rows)):
            books[t] = ob
    print(f"books {sum(1 for v in books.values() if v)}/{len(rows)} in {time.time() - t0:.0f}s", flush=True)

    def frac(y, n, own, target, df):
        return imm.estimate_reward_share(y, n, own, target, df, own_in_book=True)[0]

    def bump_level(levels, px, dq):
        out = [list(lv) for lv in levels]
        for lv in out:
            if int(lv[0]) == px:
                lv[1] += dq
                return out
        out.append([float(px), dq])
        return sorted(out, key=lambda lv: lv[0])

    out, chk = [], []
    for t, r in rows.items():
        ob = books.get(t)
        if not ob:
            continue
        y, n = imm.orderbook_levels(ob)
        own = []
        if r["ob"] > 0 and r["bt"] > 0:
            own.append(("bid", r["bt"], r["ob"]))
        if r["oa"] > 0 and 0 < r["at"] < 100:
            own.append(("ask", r["at"], r["oa"]))
        f0 = frac(y, n, own, r["target"], r["df"])
        chk.append((f0, r["est"]))
        for side, px, q in own:
            dq = a.bump * q
            if side == "bid":
                y2, n2, cost = bump_level(y, px, dq), n, px
            else:
                y2, n2, cost = y, bump_level(n, 100 - px, dq), 100 - px
            own2 = [(s, pp, qq + dq if (s, pp) == (side, px) else qq) for s, pp, qq in own]
            f1 = frac(y2, n2, own2, r["target"], r["df"])
            out.append(dict(t=t, g=group(t), side=side, px=px, cost=cost, band=band(cost), q=q, dq=dq,
                            f0=f0, f1=f1, pool=r["pool"], d_day=r["pool"] * (f1 - f0), d_risk=dq * cost / 100.0,
                            risk=q * cost / 100.0, rate=r["pool"] * f0))
    os.makedirs(os.path.dirname(a.json), exist_ok=True)
    with open(a.json, "w", encoding="utf-8") as fh:
        json.dump({"at": now.isoformat(), "bump": a.bump, "sides": out}, fh)
    d = [abs(x - y_) for x, y_ in chk]
    if d:
        print(f"recompute vs the bot's est_frac: median |diff| {statistics.median(d):.4f}, "
              f"within 0.02 on {sum(x <= 0.02 for x in d) / len(d):.0%} of {len(d)}")

    order = {lbl: i for i, (_h, lbl) in enumerate(BANDS)}
    for g in sorted({o["g"] for o in out}, key=lambda g: (g == "other bot markets", g)):
        rs = sorted([o for o in out if o["g"] == g], key=lambda o: order[o["band"]])
        agg = collections.OrderedDict()
        for o in rs:
            x = agg.setdefault(o["band"], dict(n=0, d_day=0.0, d_risk=0.0, rate=0.0, risk=0.0))
            x["n"] += 1
            x["d_day"] += o["d_day"]
            x["d_risk"] += o["d_risk"]
            x["rate"] += o["rate"] / 2
            x["risk"] += o["risk"]
        print(f"\n== {g}: +{a.bump:.0%} on one side, by what a contract ties up")
        print(f"{'':14} {'sides':>6} {'now $/day':>10} {'now $ risk':>11} {'avg/1k':>7} | "
              f"{'+$/day':>8} {'+$ risk':>9} {'marg/1k':>8}")
        for k, x in agg.items():
            print(f"{k:14} {x['n']:6d} {x['rate']:10.0f} {x['risk']:11.0f} {1000 * x['rate'] / max(x['risk'], 1):7.2f} | "
                  f"{x['d_day']:8.0f} {x['d_risk']:9.0f} {1000 * x['d_day'] / max(x['d_risk'], 1):8.2f}")
    print("\nwave series x band with +$10/day or more, best per $1k first:")
    agg2 = collections.defaultdict(lambda: [0, 0.0, 0.0])
    for o in out:
        if o["g"] in ("other listed", "other bot markets"):
            continue
        x = agg2[(o["g"], o["t"].split("-")[0], o["band"])]
        x[0] += 1
        x[1] += o["d_day"]
        x[2] += o["d_risk"]
    for (g, s, b), (n, dd, dr) in sorted(agg2.items(), key=lambda kv: -kv[1][1] / max(kv[1][2], 1)):
        if dd >= 10:
            print(f"  {g[:16]:16} {s:24} {b:12} {n:5d} sides  +${dd:7.0f}/day on +${dr:8.0f}  ({1000 * dd / dr:5.2f}/1k)")
    print(f"\nwrote {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
