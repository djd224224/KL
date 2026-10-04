"""imm_sweep_backtest.py -- read-only backtest of IMM's EVENT SWEEP BREAKER
(incentive_mm SWEEP_BREAKER, Jack 2026-10-04: "Build these 3").

    python imm_sweep_backtest.py [START END]   # days; default 2026-09-06 2026-10-04

When one of our orders in an event is swept (a fill takes all that was left of
it -- the toxic signature: full-rung fills lose ~2c/ct, partials ~0), pull
every quote we have in that event for H seconds. Scored on the bot's own logs:

  AVOIDED  our later fills in that event inside (t* + LAT, t* + H] -- marked
           out against the external mid 5 / 30 min later (cycle_log_*.csv).
           A losing fill avoided is a gain.
  COST     the event's modelled reward rate (sum over its markets of
           pool_per_day x est_frac, averaged over the day's cycles) x H x the
           share of our quotes pulled (1 = both sides, 0.5 = one side).

Variants: trigger (full-rung fill / 15s event fill volume >= N contracts,
the Kalshi order-group rule), scope (whole event / same book side), hold H,
reaction latency LAT (bot on the WS fill feed ~2s; exchange-side 0s).
A pull already in force swallows new triggers (no quotes, no fills).
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ws_stale_score as wss  # noqa: E402

D = wss.DEFAULT_DIR
START, END = (sys.argv[1], sys.argv[2]) if len(sys.argv) > 2 else ("2026-09-06", "2026-10-04")


def days(a, b):
    d = datetime.strptime(a, "%Y-%m-%d")
    while d <= datetime.strptime(b, "%Y-%m-%d"):
        yield d.strftime("%Y-%m-%d")
        d += timedelta(days=1)


def event_of(ticker):
    return ticker.rsplit("-", 1)[0]


def ts_of(s, cache={}):
    v = cache.get(s)
    if v is None:
        try:
            v = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            v = -1.0
        cache[s] = v
    return v


def load_day(day, want):
    """(mids: ticker -> [(ts, mid)], event $/day rate, cycles that day)"""
    p = os.path.join(D, f"cycle_log_{day}.csv")
    mids = defaultdict(list)
    ev_sum = defaultdict(float)
    rows_per_ticker = defaultdict(int)
    if not os.path.exists(p):
        return mids, {}, 0
    with open(p, encoding="utf-8", newline="") as f:
        idx = {}
        for row in csv.reader(f):
            if not row:
                continue
            if row[0] == "ts":
                idx = {c: i for i, c in enumerate(row)}
                continue
            if not idx:
                continue
            try:
                t = row[idx["ticker"]]
            except IndexError:
                continue
            rows_per_ticker[t] += 1
            try:
                pool = float(row[idx["pool_per_day"]] or 0)
                frac = float(row[idx["est_frac"]] or 0)
            except (ValueError, IndexError, KeyError):
                pool = frac = 0.0
            ev_sum[event_of(t)] += pool * frac
            if t in want:
                try:
                    b, a = row[idx["ext_bid"]], row[idx["ext_ask"]]
                    if b != "" and a != "":
                        mids[t].append((ts_of(row[idx["ts"]]), (float(b) + float(a)) / 2.0))
                except (ValueError, IndexError):
                    pass
    n = max(rows_per_ticker.values()) if rows_per_ticker else 0
    rate = {e: v / n for e, v in ev_sum.items()} if n else {}
    for v in mids.values():
        v.sort()
    return mids, rate, n


def mid_after(series, t):
    import bisect
    if not series:
        return None
    i = bisect.bisect_left(series, (t, -1e9))
    return series[i][1] if i < len(series) else None


# ---- load every fill with its mark-outs ----
fills = []
ev_rate_by_day = {}
for day in days(START, END):
    fp = os.path.join(D, f"fills_{day}.jsonl")
    if not os.path.exists(fp):
        continue
    raw, seen = [], set()
    with open(fp, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("is_taker") or r.get("fill_id") in seen:
                continue
            seen.add(r.get("fill_id"))
            raw.append(r)
    want = {r["ticker"] for r in raw}
    mids, rate, ncyc = load_day(day, want)
    # the next day's early rows mark late fills
    nxt = (datetime.strptime(day, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    if os.path.exists(os.path.join(D, f"cycle_log_{nxt}.csv")):
        m2, _, _ = load_day(nxt, want)
        for t, v in m2.items():
            mids[t].extend(x for x in v if x[0] < v[0][0] + 7200) if v else None
    ev_rate_by_day[day] = rate
    for r in raw:
        px = float(r["yes_price_cents"])
        n = float(r["count"])
        sgn = 1.0 if r.get("our_book_side") == "bid" else -1.0
        pnl = {}
        for h in (300, 1800):
            m = mid_after(mids.get(r["ticker"]), float(r["ts"]) + h)
            pnl[h] = None if m is None else sgn * (m - px) * n / 100.0
        fills.append({
            "day": day, "ts": float(r["ts"]), "ticker": r["ticker"],
            "event": r.get("event_ticker") or event_of(r["ticker"]),
            "side": r.get("our_book_side"), "n": n,
            "full": float(r.get("our_remaining_before") or 0) <= n + 1e-9,
            "pnl": pnl})
    print(f"{day}: {len(raw)} fills, {ncyc} cycles", file=sys.stderr)

fills.sort(key=lambda f: f["ts"])
ndays = len(ev_rate_by_day)
by_event = defaultdict(list)
for f in fills:
    by_event[f["event"]].append(f)
base5 = sum(f["pnl"][300] or 0 for f in fills)
base30 = sum(f["pnl"][1800] or 0 for f in fills)
print(f"\n{len(fills)} fills over {ndays} days; all fills mark out "
      f"5m ${base5 / ndays:+.0f}/day, 30m ${base30 / ndays:+.0f}/day\n")


def run(trigger, scope, H, LAT, vol_n=None):
    trig = avoided_n = 0
    av_ct = av5 = av30 = cost = 0.0
    for ev, fs in by_event.items():
        pull_start = pull_until = -1.0
        trig_side = None
        for i, f in enumerate(fs):
            t = f["ts"]
            if t <= pull_until and t >= pull_start + LAT:
                if scope == "event" or f["side"] == trig_side:
                    avoided_n += 1
                    av_ct += f["n"]
                    av5 += f["pnl"][300] or 0.0
                    av30 += f["pnl"][1800] or 0.0
                    continue
            if t <= pull_until:
                continue          # inside the reaction latency, or other side
            fire = False
            if trigger == "full":
                fire = f["full"]
            elif trigger == "vol":
                vol = sum(g["n"] for g in fs[:i + 1] if t - 15 < g["ts"] <= t)
                fire = vol >= vol_n
            if fire:
                trig += 1
                pull_start, pull_until, trig_side = t, t + H, f["side"]
                rate = ev_rate_by_day.get(f["day"], {}).get(ev, 0.0)
                cost += rate / 86400.0 * H * (1.0 if scope == "event" else 0.5)
    return {"trig": trig / ndays, "fills": avoided_n / ndays, "ct": av_ct / ndays,
            "gain5": -av5 / ndays, "gain30": -av30 / ndays, "cost": cost / ndays,
            "net5": (-av5 - cost) / ndays, "net30": (-av30 - cost) / ndays}


print(f"{'trigger':18s} {'scope':6s} {'H':>5s} {'lat':>3s} | {'trig/d':>6s} {'fills/d':>7s} "
      f"{'ct/d':>6s} | {'avoid5':>7s} {'avoid30':>7s} {'cost':>6s} | {'net5':>7s} {'net30':>7s}")
for trigger, vol_n in (("full", None), ("vol", 20), ("vol", 50), ("vol", 100)):
    for scope in ("event", "side"):
        for H in (30, 60, 120, 300, 600):
            for LAT in ((0, 2) if trigger == "vol" else (2,)):
                r = run(trigger, scope, H, LAT, vol_n)
                name = "full-rung fill" if trigger == "full" else f"15s vol >= {vol_n}"
                print(f"{name:18s} {scope:6s} {H:5d} {LAT:3d} | {r['trig']:6.1f} {r['fills']:7.1f} "
                      f"{r['ct']:6.0f} | ${r['gain5']:+6.1f} ${r['gain30']:+6.1f} ${r['cost']:5.1f} | "
                      f"${r['net5']:+6.1f} ${r['net30']:+6.1f}")
