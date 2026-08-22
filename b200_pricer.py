"""KXB200WS fair-value pricer — research tool, READ-ONLY (places no orders).

Fetches the Ornn B200 daily index (free 3-mo API) and live Kalshi KXB200WS
books, runs the EW-drift momentum model, and prints a fair curve + edge table
per open weekly event with suggested passive/taker entries and EV after fees.

Model (validated walk-forward vs settled weeks, see B200_STRATEGY.md):
  - Settlement = Ornn's Friday 20:00Z daily print (proven vs JUL10/AUG07).
  - E[Friday] = last print + EW-mean daily drift (halflife IMM 6d), weekends
    damped by empirical weekday vol weights.
  - Uncertainty: Student-t(4), residual sd around drift, weekday-weighted,
    inflated by sd_mult (default 1.25).

Usage:
  python b200_pricer.py            # fair curve + edge table, all open weeks
  python b200_pricer.py --week 26AUG14
  python b200_pricer.py --halflife 6 --sd-mult 1.25 --min-edge 0.05

Env: none required. No API keys. Kalshi public endpoints only.
"""
import argparse
import datetime as dt
import json
import math
import statistics as st
import time
import urllib.request

ORNN = "https://dashboard.ornnai.com/api/gpu/B200/index-history"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
UA = {"User-Agent": "Mozilla/5.0"}

# empirical per-weekday sd of daily index changes (Mon..Sun), May-Aug 2026
WD_SD = {0: 0.19, 1: 0.26, 2: 0.24, 3: 0.19, 4: 0.16, 5: 0.13, 6: 0.06}
BASE_SD = 0.19
FEE_MULT = 0.07  # Kalshi quadratic taker fee


def get(url, tries=5):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(12 * (i + 1))
                continue
            raise
    raise RuntimeError("429s exhausted: " + url)


def load_index():
    d = get(ORNN)
    pts = sorted(
        (dt.datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")).date(), p["index_value"])
        for p in d["data"]
    )
    return pts


def t_cdf(x, df=4):
    if x == 0:
        return 0.5
    p = 0.5 * _ibeta(df / 2.0, 0.5, df / (df + x * x))
    return 1 - p if x > 0 else p


def _ibeta(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(math.log(x) * a + math.log(1 - x) * b - lbeta) / a
    f, c, d = 1.0, 1.0, 0.0
    for i in range(200):
        m = i // 2
        if i == 0:
            num = 1.0
        elif i % 2 == 0:
            num = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            num = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        d = 1.0 / (d if abs(d) > 1e-30 else 1e-30)
        c = 1.0 + num / (c if abs(c) > 1e-30 else 1e-30)
        f *= c * d
        if abs(1.0 - c * d) < 1e-10:
            break
    return front * f


def forecast(idx, target_fri, halflife=6.0, sd_mult=1.25):
    asof, last = idx[-1]
    lam = math.log(2) / halflife
    wsum = dsum = 0.0
    vals = [v for _, v in idx]
    for i in range(1, len(idx)):
        w = math.exp(-lam * (asof - idx[i][0]).days)
        wsum += w
        dsum += w * (vals[i] - vals[i - 1])
    drift = dsum / wsum if wsum else 0.0
    resid = []
    for i in range(max(1, len(vals) - 45), len(vals)):
        wd = idx[i][0].weekday()
        resid.append((vals[i] - vals[i - 1] - drift) / (WD_SD[wd] / BASE_SD))
    sd_day = st.pstdev(resid) if len(resid) > 5 else 0.19
    mean, var, d = last, 0.0, asof
    while d < target_fri:
        d += dt.timedelta(days=1)
        w = WD_SD[d.weekday()] / BASE_SD
        mean += drift * w
        var += (sd_day * w) ** 2
    return mean, math.sqrt(var) * sd_mult, drift


def p_above(k, mean, sd, df=4):
    return 1 - t_cdf((k + 0.005 - mean) / max(sd, 1e-6), df)


def taker_fee(p):
    return math.ceil(FEE_MULT * p * (1 - p) * 100) / 100


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", help="event date part, e.g. 26AUG14 (default: all open)")
    ap.add_argument("--halflife", type=float, default=6.0)
    ap.add_argument("--sd-mult", type=float, default=1.25)
    ap.add_argument("--min-edge", type=float, default=0.05, help="min |model - price| to flag")
    args = ap.parse_args()

    idx = load_index()
    asof, last = idx[-1]
    _, _, drift = forecast(idx, asof + dt.timedelta(days=1), args.halflife, args.sd_mult)
    print(f"Ornn B200 index: last={last} ({asof}, {asof.strftime('%a')})  EW drift={drift:+.3f}/day")
    print("recent prints: " + "  ".join(f"{d.strftime('%m/%d')}:{v}" for d, v in idx[-8:]))

    mk = get(KALSHI + "/markets?series_ticker=KXB200WS&status=open&limit=1000")["markets"]
    events = sorted({m["event_ticker"] for m in mk})
    for ev in events:
        wk = ev.split("-")[1]
        if args.week and wk != args.week:
            continue
        ms = sorted((m for m in mk if m["event_ticker"] == ev), key=lambda m: float(m["floor_strike"]))
        close = dt.datetime.fromisoformat(ms[0]["close_time"].replace("Z", "+00:00"))
        fri = close.date()
        mean, sd, _ = forecast(idx, fri, args.halflife, args.sd_mult)
        days_left = (fri - asof).days
        print(f"\n== {ev}  settles {fri} ({days_left}d)  model E={mean:.2f} sd={sd:.2f} ==")
        print(f"{'strike':>7} {'bid/ask':>12} {'model':>6} {'edge':>6}  suggestion")
        for m in ms:
            k = float(m["floor_strike"])
            yb = float(m.get("yes_bid_dollars") or 0)
            ya = float(m.get("yes_ask_dollars") or 1)
            pm = p_above(k, mean, sd)
            mid = (yb + ya) / 2
            edge = pm - mid
            sug = ""
            if abs(edge) >= args.min_edge and 0.01 <= mid <= 0.99:
                if edge > 0:
                    ev_take = pm - ya - taker_fee(ya)
                    sug = f"BUY YES  take@{ya:.2f} (EV {ev_take:+.2f})  or rest {min(ya - 0.01, round(pm - args.min_edge, 2)):.2f}"
                else:
                    no_cost = 1 - yb
                    ev_take = (1 - pm) - no_cost - taker_fee(yb)
                    sug = f"BUY NO   take@{no_cost:.2f} (EV {ev_take:+.2f})  or rest ask {max(yb + 0.01, round(pm + args.min_edge, 2)):.2f}"
            print(f"{k:7.2f} {yb:5.2f}/{ya:5.2f} {pm:6.1%} {edge:+6.1%}  {sug}")


if __name__ == "__main__":
    main()
