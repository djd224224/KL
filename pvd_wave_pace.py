#!/usr/bin/env python3
"""PACE / RESIZE / WHYDARK for registry waves (the market-wave skill, 2026-11-14; Jack
"Yes, do them" on the skill update). The 11/13-11/14 short waves asked the same three
questions over and over -- "what % of market wave is pacing below $1 threshold", "size
them up", "of whatever isnt quoting, breakdown why" -- and each answer was a scratch
script in some session's scratchpad. These are the tested versions, run through
pvd_waves.py:

  pvd_waves.py pace NAME [--aim 1.20] [--window-min 60] [--json OUT]
  pvd_waves.py resize NAME [--aim 1.20] [--max-mult 5] [--real 0.65] [--keep-within 0.05]
                           [--apply] [--force]
  pvd_waves.py whydark [NAME|all] [--json OUT]

PACE (read-only). A listed market's projection for the period = what it banked so far
(the wave dashboard's period_est) + its AVERAGE reward rate since its size landed x the
hours left. The average, not one cycle: the bot's per-cycle share swings ~2% <-> 9% on
wide books and reads 0 for a cycle after a fair stand-down (w1113, 11/14 13:45Z), so a
single read overstated the under-$1 count. The rate is weighted by how often the market
actually rests (a cycle with nothing resting counts 0). Rows come from the bot's cycle
log from SETTLE_MIN after its last start; a market resting under LANDED_FRAC of what the
bot wants is "landing" and not judged. Reads within READ_LOW_MIN of a restart run low
(11/14 04:55Z: the crypto wave read 160 strikes at $1+ seven minutes after a restart,
230-286 before): pace says so, and resize refuses without --force.

THE MULTIPLE NEEDED for --aim ($1.20: Jack 2026-11-11 "in general as a rule of thumb when
sizing to hit threshold, aim for $1.2"). Our share s of a side grows with our size x r as
s' = r s / (1 + (r - 1) s) (others' size standing still), and only --real (65%, the
runbook's measured 55-75%) of the gain is counted. That is the crypto wave's model, and
it still OVERPROMISED (11/14 04:55Z: 45 of 108 pinned strikes at $1+ against "enough" for
all): Kalshi scores only the 1,000-contract window from the best price, and other makers
answer with size (FX 05:30-07:00Z: ~5 contracts for each of ours). Every multiple is an
upper bound: re-measure once the sizes have rested.

RESIZE writes the wave's per-market multiples file (run-logs/pvd-scan/size_mult_NAME.json,
read by its tune's --size-mult-file; 0 = off, the sizes kept in "pre_off"):
  * a market under the aim gets the smallest quarter step up to --max-mult (5x: Jack
    2026-11-14 "actually go up to 5x every market, to get them over the $1 threshold")
    whose projection reaches the aim, else --max-mult if that still reaches $1; it never
    lowers a multiple;
  * a market that can't reach $1 even at --max-mult goes to 0 (Jack 2026-11-14: "after
    these increases, stop quoting markets still pacing <$1") -- only when the previous
    resize read, 5-90 min earlier, judged it the same way (two reads: one read just after a
    restart would have stopped 258 strikes on 11/14), and only if it projects under
    $1 - --keep-within (a $0.95-1.00 market is inside the estimate's noise);
  * an off market stays off (Jack 2026-11-14 13:40Z on the FX offs: "Keep them off");
  * without --apply it only prints the plan (and records the read for the two-read rule);
    --apply writes the file, adds --size-mult-file to the wave's tune if it is missing, and
    runs the wave now. Sizing a wave up is Jack's word -- except the $1-floor protocol on a
    short-window live-price wave with a live fair, which he pre-approved 2026-11-14 ("Yes,
    pre-approve"; manual mode only, never above 5x).

WHYDARK (read-only): per listed live market of the wave, two-sided / one-sided / dark on
Kalshi, and for every side not resting the first reason that holds, in this order: on
purpose (wave off, paused, sized 0), closed on Kalshi, held (only the closing side rests),
a breaker / halt hold, the bot's selection decision, a bot guard on the market, the bot
log's per-side lines (crypto stand-aside, lone-ref guard, pvd guard limits), the book past
the side's limit (fair clamp or the capped re-anchor), wanted but queued (placement cap),
sized 0 by the bot, else unexplained. darkcheck answers "is a wave dark"; whydark answers
"why isn't this side quoting".
"""
from __future__ import annotations

import collections
import json
import math
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import pvd_waves as pw

LANDED_FRAC = 0.9          # resting >= this x the bot's wanted size = its size has landed
SETTLE_MIN = 10.0          # cycle rows count from this long after the bot's latest start
READ_LOW_MIN = 15.0        # reads within this long of a restart run low
DASH_STALE_MIN = 12.0      # a dashboard read older than this is stale
MULT_STEP = 0.25
TWO_READ_GAP_MIN = (5.0, 90.0)
COST_BUCKETS = [(2, "1-2c"), (5, "3-5c"), (10, "6-10c"), (20, "11-20c"), (35, "21-35c"), (50, "36-50c"),
                (100, "51c+")]
MULT_BUCKETS = [(1.5, "1.5x or less"), (2.0, "1.5-2x"), (3.0, "2-3x"), (5.0, "3-5x"), (10.0, "5-10x"),
                (float("inf"), "10x+")]
PROJ_BUCKETS = [(1.30, "$1.30+"), (1.00, "$1.00-1.30"), (0.75, "$0.75-1.00"), (0.50, "$0.50-0.75"),
                (0.25, "$0.25-0.50"), (0.0, "under $0.25")]


def size_mult_path(name: str) -> str:
    return os.path.join(pw.HERE, "run-logs", "pvd-scan", f"size_mult_{name}.json")


def resize_state_path(name: str) -> str:
    return os.path.join(pw.HERE, "run-logs", "pvd-scan", f"resize_state_{name}.json")


def _ts(s):
    return pw._ts_of(s)


def dash_data(w: dict) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{w['port']}/data", timeout=90) as r:
        d = json.load(r)
    return d.get("data", d)


def cycle_rows(tickers, since_ts: float, now: datetime) -> dict:
    """{ticker: [row]} from the bot's cycle logs (UTC-dated files) at or after since_ts.
    A row = {ts, b, a, wb, wa, rate, pool} (own and wanted contracts a side, the bot's
    est_frac x pool_per_day, pool_per_day)."""
    want = set(tickers)
    out: dict = collections.defaultdict(list)
    days = sorted({datetime.fromtimestamp(since_ts, timezone.utc).date(), now.date()})
    for d in days:
        p = os.path.join(pw.INC, f"cycle_log_{d.isoformat()}.csv")
        if not os.path.exists(p):
            continue
        size = os.path.getsize(p)
        with open(p, "rb") as fh:
            hdr = fh.readline().decode("utf-8", "replace").strip().split(",")
            ix = {k: i for i, k in enumerate(hdr)}
            need = ("ts", "ticker", "est_frac", "pool_per_day", "own_bid_ct", "own_ask_ct", "want_bid_ct",
                    "want_ask_ct")
            if any(k not in ix for k in need):
                continue
            # read back from the end until the chunk starts before since_ts
            n = 32_000_000
            while True:
                start = max(0, size - n)
                fh.seek(start)
                if start:
                    fh.readline()
                first = fh.readline()
                t0 = _ts(first[:25].decode("utf-8", "replace")) if first else None
                if start == 0 or t0 is None or t0 <= since_ts or n >= 512_000_000:
                    break
                n *= 2
            fh.seek(start)
            if start:
                fh.readline()
            data = fh.read().decode("utf-8", "replace").splitlines()
        cut = datetime.fromtimestamp(since_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        width = len(hdr)
        for ln in data:
            q = ln.split(",")
            if len(q) < width or q[ix["ticker"]] not in want or q[ix["ts"]][:19] < cut:
                continue

            def f(k):
                v = q[ix[k]]
                try:
                    return float(v) if v not in ("", None) else 0.0
                except ValueError:
                    return 0.0
            pool = f("pool_per_day")
            out[q[ix["ticker"]]].append({"ts": q[ix["ts"]][:19], "b": f("own_bid_ct"), "a": f("own_ask_ct"),
                                         "wb": f("want_bid_ct"), "wa": f("want_ask_ct"),
                                         "rate": f("est_frac") * pool, "pool": pool})
    return out


def judge_rows(rows: list) -> dict:
    """The market's measured state from its rows (oldest first): status, the average rate
    (resting-weighted), uptime, share while resting, the latest own / wanted contracts."""
    if not rows:
        return {"status": "no rows"}
    last = rows[-1]
    own_l, want_l = last["b"] + last["a"], last["wb"] + last["wa"]
    # LANDING = resting some, but under LANDED_FRAC of what the bot wants (a size step not
    # rested yet). Resting nothing is not landing: that cycle simply earns 0 (a stand-down,
    # a queued side, a breaker) and lowers the uptime.
    landing = lambda o, wn: wn > 0 and 0 < o < LANDED_FRAC * wn
    if landing(own_l, want_l):
        return {"status": "landing", "own": own_l, "want": want_l}
    i0 = 0
    for i in range(len(rows) - 1, -1, -1):
        if landing(rows[i]["b"] + rows[i]["a"], rows[i]["wb"] + rows[i]["wa"]):
            break
        i0 = i
    use = rows[i0:]
    rest = [r for r in use if r["b"] + r["a"] > 0]
    uptime = len(rest) / len(use)
    rate_eff = sum(r["rate"] for r in rest) / len(use)
    pool = max((r["pool"] for r in use), default=0.0)
    share = (sum(r["rate"] for r in rest) / len(rest) / pool) if rest and pool > 0 else 0.0
    n_rest = max(len(rest), 1)
    return {"status": "judged" if rest else "not resting", "rate": rate_eff, "uptime": uptime, "share": share,
            "pool": pool, "own": own_l, "want": want_l, "rows": len(use),
            # two-sided = both sides rested in most of the rows since it landed
            "two_sided": sum(1 for r in use if r["b"] > 0 and r["a"] > 0) >= 0.5 * len(use),
            # the average size a side rests while the market rests (for the cash a size-up adds)
            "own_b": sum(r["b"] for r in rest) / n_rest, "own_a": sum(r["a"] for r in rest) / n_rest}


def proj_at(r: float, j: dict, banked: float, hrs: float, real: float) -> float:
    """The period projection with our size x r (share model in the module docstring)."""
    s = min(max(j.get("share") or 0.0, 0.0), 0.999)
    s2 = r * s / (1 + (r - 1) * s) if s > 0 else 0.0
    rate = (j.get("pool") or 0.0) * (s + real * (s2 - s)) * (j.get("uptime") or 0.0)
    return banked + rate * hrs / 24


def needed_mult(j: dict, banked: float, hrs: float, m0: float, aim: float, max_mult: float, real: float):
    """(the smallest quarter-step multiple <= max_mult reaching `aim`, or None; the
    projection at max_mult)."""
    m0 = m0 or 1.0
    top = max(max_mult, m0)
    p_top = proj_at(top / m0, j, banked, hrs, real)
    if proj_at(1.0, j, banked, hrs, real) >= aim:
        return round(m0, 2), p_top
    if p_top < aim:
        return None, p_top
    # the projection rises with size: bisect over the quarter steps above m0
    lo = math.floor(m0 / MULT_STEP + 1e-9) + 1          # the first quarter step above m0
    hi = math.floor(top / MULT_STEP + 1e-9)
    if hi < lo:
        return round(top, 2), p_top
    while lo < hi:
        mid = (lo + hi) // 2
        if proj_at(mid * MULT_STEP / m0, j, banked, hrs, real) >= aim:
            hi = mid
        else:
            lo = mid + 1
    x = lo * MULT_STEP
    return (round(x, 2) if proj_at(x / m0, j, banked, hrs, real) >= aim else round(top, 2)), p_top


def _bucket(v: float, buckets) -> str:
    for hi, lab in buckets:
        if v <= hi:
            return lab
    return buckets[-1][1]


def _proj_bucket(p: float) -> str:
    for lo, lab in PROJ_BUCKETS:
        if p >= lo:
            return lab
    return PROJ_BUCKETS[-1][1]


def side_costs(row: dict) -> dict:
    """{side: cents a contract a fill ties up} from the dashboard row (our price, else the touch)."""
    out = {}
    b = row.get("our_bid") if row.get("our_bid") is not None else row.get("bid")
    a = row.get("our_ask") if row.get("our_ask") is not None else row.get("ask")
    try:
        if b is not None and float(b) > 0:
            out["bid"] = float(b)
        if a is not None and 0 < float(a) < 100:
            out["ask"] = 100 - float(a)
    except (TypeError, ValueError):
        pass
    return out


def wave_tickers(name: str, w: dict, L: dict, now: datetime) -> dict:
    mine = set(pw.all_series(w))
    nts = now.timestamp()
    return {t: e for t, e in L.items() if t.split("-")[0] in mine and (_ts(e.get("stop")) or 0) > nts}


def measure(name: str, a, now: "datetime | None" = None, dash=None, rows=None, bot=None, mult=None, L=None) -> dict:
    """Everything pace and resize need (tests pass dash / rows / bot / mult / L)."""
    now = now or datetime.now(timezone.utc)
    nts = now.timestamp()
    reg = pw.load()
    w = reg["waves"].get(name)
    if not w:
        raise SystemExit(f"no wave {name}")
    if L is None:
        with open(pw.LIST, encoding="utf-8") as fh:
            L = json.load(fh).get("markets") or {}
    listed = wave_tickers(name, w, L, now)
    dash = dash if dash is not None else dash_data(w)
    drows = {r["ticker"]: r for r in dash.get("rows") or [] if r.get("ticker") in listed}
    bot = bot if bot is not None else pw.bot_status(now)
    started = bot.get("started") or 0.0
    if mult is None:
        try:
            with open(size_mult_path(name), encoding="utf-8") as fh:
                mult = {str(k): float(v) for k, v in (json.load(fh) or {}).items()}
        except (OSError, ValueError):
            mult = {}
    since = max(nts - a.window_min * 60, started + SETTLE_MIN * 60)
    rows = rows if rows is not None else cycle_rows(listed, since, now)
    dash_at = _ts(dash.get("now"))
    res = {"wave": name, "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "dash_at": dash.get("now"),
           "dash_age_min": round((nts - dash_at) / 60, 1) if dash_at else None,
           "bot_started": datetime.fromtimestamp(started, timezone.utc).strftime("%H:%M:%SZ") if started else None,
           "since_restart_min": round((nts - started) / 60, 1) if started else None,
           "rows_since": datetime.fromtimestamp(since, timezone.utc).strftime("%H:%M:%SZ"),
           "aim": a.aim, "max_mult": getattr(a, "max_mult", 5.0), "real": getattr(a, "real", 0.65),
           "listed": len(listed), "markets": []}
    for t in sorted(listed):
        dr = drows.get(t) or {}
        m0 = mult.get(t, 1.0)
        j = judge_rows(rows.get(t) or [])
        banked = float(dr.get("period_est") or 0.0)
        hrs = float(dr.get("hrs_left") if dr.get("hrs_left") is not None else
                    max(0.0, ((_ts(listed[t].get("stop")) or nts) - nts) / 3600))
        m = {"ticker": t, "series": t.split("-")[0], "m0": m0, "banked": round(banked, 3), "hrs": round(hrs, 2),
             "status": "off" if m0 == 0 else j["status"], "costs": side_costs(dr)}
        if m["status"] == "judged":
            proj = proj_at(1.0, j, banked, hrs, 0.0)
            x, p_max = needed_mult(j, banked, hrs, m0, a.aim, res["max_mult"], res["real"])
            m.update(proj=round(proj, 3), rate=round(j["rate"], 4), share=round(j["share"], 4),
                     uptime=round(j["uptime"], 3), pool=round(j["pool"], 3), two_sided=j["two_sided"],
                     own_b=j["own_b"], own_a=j["own_a"], x_aim=x, proj_max=round(p_max, 3))
            # the multiple the aim needs with no cap (for the shortfall table)
            xx, _p = needed_mult(j, banked, hrs, m0, a.aim, 1000.0, res["real"])
            m["x_aim_uncapped"] = xx
        elif m["status"] in ("landing",):
            m.update(own=j.get("own"), want=j.get("want"))
        res["markets"].append(m)
    return res


def print_pace(res: dict) -> None:
    ms = res["markets"]
    j = [m for m in ms if m["status"] == "judged"]
    under = [m for m in j if m["proj"] < 1.0]
    low = res.get("since_restart_min") is not None and res["since_restart_min"] < READ_LOW_MIN
    print(f"pace {res['wave']} {res['at']}: dashboard {res['dash_at']} ({res['dash_age_min']} min old); bot started "
          f"{res['bot_started']} ({res['since_restart_min']} min ago); cycle rows since {res['rows_since']}")
    if low:
        print(f"  ! within {READ_LOW_MIN:.0f} min of a restart: reads run low -- judge sizes later")
    if res["dash_age_min"] is not None and res["dash_age_min"] > DASH_STALE_MIN:
        print(f"  ! the dashboard read is {res['dash_age_min']} min old")
    st = collections.Counter(m["status"] for m in ms)
    print(f"  {res['listed']} listed: {st.get('judged', 0)} judged, {st.get('landing', 0)} landing (size not rested "
          f"yet), {st.get('not resting', 0)} not resting, {st.get('no rows', 0)} with no bot rows, "
          f"{st.get('off', 0)} off (multiple 0)")
    if j:
        tot_rate = sum(m["rate"] for m in j)
        banked = sum(m["banked"] for m in ms)
        paid = sum(m["proj"] for m in j if m["proj"] >= 1.0)
        print(f"  rate (average since landed) ${tot_rate:,.0f}/day; banked so far ${banked:,.0f}; "
              f"{len(j) - len(under)} of {len(j)} judged project $1+ (worth ~${paid:,.0f} by the stop)")
    print("\n  Projected this period (average share since the size landed):")
    pb = collections.Counter(_proj_bucket(m["proj"]) for m in j)
    for _lo, lab in PROJ_BUCKETS:
        print(f"    {lab:12} {pb.get(lab, 0):6}")
    if not under:
        return
    print(f"\n  Under $1: {len(under)} -- the multiple each needs for ${res['aim']:.2f} (model: an UPPER bound)")
    print(f"    {'multiple':14} {'markets':>7} {'one-sided':>9} {'median share':>12} {'+ $ if all filled':>17}")
    groups = collections.defaultdict(list)
    for m in under:
        x = m.get("x_aim_uncapped")
        r = (x / (m["m0"] or 1.0)) if x else None
        lab = "can't (0 share)" if not m.get("share") else (_bucket(r, MULT_BUCKETS) if r else "10x+")
        groups[lab].append((m, r))
    for _hi, lab in MULT_BUCKETS + [(0, "can't (0 share)")]:
        g = groups.get(lab) or []
        if not g:
            continue
        shares = sorted(m["share"] for m, _r in g)
        extra = 0.0
        for m, r in g:
            if r:
                for sd, c in (m.get("costs") or {}).items():
                    q = m.get("own_b" if sd == "bid" else "own_a") or 0
                    extra += (r - 1) * q * c / 100
        print(f"    {lab:14} {len(g):7} {sum(1 for m, _r in g if not m.get('two_sided')):9} "
              f"{shares[len(shares) // 2] * 100:11.1f}% {'$' + format(extra, ',.0f'):>17}")
    print(f"\n  Under $1 by side cost (what one filled contract ties up):")
    cb = collections.Counter()
    for m in under:
        for sd, c in (m.get("costs") or {}).items():
            cb[_bucket(c, COST_BUCKETS)] += 1
    for _hi, lab in COST_BUCKETS:
        if cb.get(lab):
            print(f"    {lab:8} {cb[lab]:6} sides")
    print(f"\n  Under $1 by series:")
    bs = collections.Counter(m["series"] for m in under)
    tot = collections.Counter(m["series"] for m in j)
    for s, n in bs.most_common():
        print(f"    {s:22} {n:5} of {tot[s]} judged")


def cmd_pace(a) -> int:
    res = measure(a.name, a)
    print_pace(res)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
    return 0


def plan_resize(res: dict, prev: dict, a, now: datetime) -> dict:
    """{"up": {t: x}, "off": [t], "off_next": [t], "keep": n, ...} from measure()'s result
    and the previous resize read (the two-read rule)."""
    nts = now.timestamp()
    prev_at = _ts(prev.get("at"))
    gap = (nts - prev_at) / 60 if prev_at else None
    prev_ok = gap is not None and TWO_READ_GAP_MIN[0] <= gap <= TWO_READ_GAP_MIN[1]
    prev_off = set(prev.get("off_candidates") or []) if prev_ok else set()
    out = {"up": {}, "off": [], "off_next": [], "keep": 0, "at_max": 0, "borderline": 0, "skipped": collections.Counter(),
           "prev_gap_min": round(gap, 1) if gap is not None else None, "candidates": []}
    for m in res["markets"]:
        if m["status"] != "judged":
            out["skipped"][m["status"]] += 1
            continue
        if m["proj"] >= a.aim:
            out["keep"] += 1
            continue
        x, m0 = m.get("x_aim"), m["m0"] or 1.0
        if x is not None:
            if x > m0 + 1e-9:
                out["up"][m["ticker"]] = x
            else:
                out["keep"] += 1
            continue
        if m["proj_max"] >= 1.0:
            if a.max_mult > m0 + 1e-9:
                out["up"][m["ticker"]] = a.max_mult      # reaches $1, not the aim: as far as the cap goes
            else:
                out["at_max"] += 1
            continue
        if m["proj"] >= 1.0 - a.keep_within:
            out["borderline"] += 1                      # inside the noise: keeps its banked share
            continue
        if a.no_off:
            out["skipped"]["can't reach $1 (--no-off)"] += 1
            continue
        out["candidates"].append(m["ticker"])
        (out["off"] if m["ticker"] in prev_off else out["off_next"]).append(m["ticker"])
    return out


def cmd_resize(a) -> int:
    now = datetime.now(timezone.utc)
    res = measure(a.name, a, now=now)
    print_pace(res)
    low = res.get("since_restart_min") is not None and res["since_restart_min"] < READ_LOW_MIN
    stale = res["dash_age_min"] is None or res["dash_age_min"] > DASH_STALE_MIN
    sp = resize_state_path(a.name)
    try:
        with open(sp, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        prev = {}
    plan = plan_resize(res, prev, a, now)
    print(f"\nresize plan (aim ${a.aim:.2f}, up to {a.max_mult:g}x, {a.real:.0%} of the modelled gain): "
          f"{len(plan['up'])} up, {len(plan['off'])} off now (under $1 at {a.max_mult:g}x on two reads), "
          f"{len(plan['off_next'])} off at the next read if still short, {plan['keep']} keep, "
          f"{plan['borderline']} borderline kept, {plan['at_max']} already at the cap; skipped "
          f"{dict(plan['skipped'])}; previous read {plan['prev_gap_min']} min ago")
    by = collections.Counter(round(x, 2) for x in plan["up"].values())
    if by:
        print("  new multiples: " + ", ".join(f"{k:g}x {v}" for k, v in sorted(by.items())))
    # the read is recorded for the two-read rule even on a dry run (a read is a read)
    if not low or a.force:
        with open(sp + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"at": res["at"], "off_candidates": plan["candidates"], "applied": bool(a.apply)}, fh, indent=1)
        os.replace(sp + ".tmp", sp)
    if not a.apply:
        print("dry run: nothing written (--apply writes the multiples file and runs the wave: pre-approved on a "
              "type B wave with a live fair, else Jack's word)")
        return 0
    if (low or stale) and not a.force:
        print(f"NOT APPLIED: {'a restart ' + str(res['since_restart_min']) + ' min ago' if low else ''}"
              f"{' and ' if low and stale else ''}{'a stale dashboard read' if stale else ''} -- reads run low; "
              f"re-run later or pass --force")
        return 1
    mp = size_mult_path(a.name)
    try:
        with open(mp, encoding="utf-8") as fh:
            cur = {str(k): float(v) for k, v in (json.load(fh) or {}).items()}
    except (OSError, ValueError):
        cur = {}
    n_up = n_off = 0
    for t, x in plan["up"].items():
        if cur.get(t, 1.0) != 0 and x > cur.get(t, 1.0) + 1e-9:     # never lowers; an off stays off
            cur[t] = x
            n_up += 1
    for t in plan["off"]:
        if cur.get(t, 1.0) != 0:
            cur[t] = 0
            n_off += 1
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    with open(mp + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=0, sort_keys=True)
    os.replace(mp + ".tmp", mp)
    print(f"WROTE {mp}: {n_up} up, {n_off} off ({sum(1 for v in cur.values() if v > 0)} sized, "
          f"{sum(1 for v in cur.values() if v == 0)} off in the file)")
    w = pw.load()["waves"][a.name]
    if mp not in pw._vals(w.get("tune") or [], "--size-mult-file"):
        r = subprocess.run([pw.PY, os.path.join(pw.HERE, "pvd_waves.py"), "setarg", a.name, "size-mult-file", mp],
                           cwd=pw.HERE, capture_output=True, text=True)
        print((r.stdout + r.stderr).strip()[:300])
    r = subprocess.run([pw.PY, os.path.join(pw.HERE, "pvd_waves.py"), "run", "--only", a.name, "--write"],
                       cwd=pw.HERE, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print("\n".join(ln for ln in (r.stdout + r.stderr).splitlines() if "WROTE" in ln or "skipped" in ln
                    or "waiting" in ln)[-600:])
    print("the sizes land over the next cycles (amend cap, a +25% step waits for its ~25-min re-placement); "
          "re-run `pace` after they rest")
    return 0


# ---------------------------------------------------------------------------
# WHYDARK
# ---------------------------------------------------------------------------
LOG_TAIL_BYTES = 12_000_000
LOG_WINDOW_MIN = 30.0
_RX_PVD = re.compile(r"\] pvd guard (\S+): ((?:bid|ask)(?:/(?:bid|ask))?) out \(([^)]*)\)")
_RX_PVD_CLEAR = re.compile(r"\] pvd guard (\S+): clear")
_RX_LONE = re.compile(r"\] lone-ref guard(?: \(([^)]*)\))? (\S+) (bid|ask): (.*)$")
_RX_CRYPTO = re.compile(r"\] crypto-event stand-aside (\S+): (.*)$")
_RX_CRYPTO_RES = re.compile(r"\] crypto-event resume (\S+)")


def log_side_reasons(tickers, now: datetime) -> dict:
    """{(ticker, side): reason} from the newest bot log's last LOG_WINDOW_MIN; a later
    "clear" / "resume" line drops it."""
    want = set(tickers)
    logs = sorted(pw.glob.glob(os.path.join(pw.INC, "incentive-mm-20*.log")), key=os.path.getmtime)
    out: dict = {}
    if not logs:
        return out
    cut = (now - timedelta(minutes=LOG_WINDOW_MIN)).strftime("%Y-%m-%d %H:%M:%S")
    for ln in pw._tail_lines(logs[-1], LOG_TAIL_BYTES):
        if ln[:19] < cut:
            continue
        m = _RX_PVD.search(ln)
        if m and m.group(1) in want:
            for sd in m.group(2).split("/"):
                out[(m.group(1), sd)] = f"wave limits keep it out ({m.group(3)[:80]})"
            continue
        m = _RX_PVD_CLEAR.search(ln)
        if m and m.group(1) in want:
            out.pop((m.group(1), "bid"), None)
            out.pop((m.group(1), "ask"), None)
            continue
        m = _RX_LONE.search(ln)
        if m and m.group(2) in want:
            k = (m.group(2), m.group(3))
            txt = m.group(4)
            if txt.strip() == "clear":
                out.pop(k, None)
            elif "would stand aside" not in txt:
                out[k] = "lone-ref guard: its reference is a lone thin top (stands aside while it lasts)"
            continue
        m = _RX_CRYPTO.search(ln)
        if m and m.group(1) in want:
            for sd in ("bid", "ask"):
                out[(m.group(1), sd)] = f"crypto fair stands it aside ({m.group(2)[:70]})"
            continue
        m = _RX_CRYPTO_RES.search(ln)
        if m and m.group(1) in want:
            out.pop((m.group(1), "bid"), None)
            out.pop((m.group(1), "ask"), None)
    return out


def latest_cycle(tickers, now: datetime) -> dict:
    rows = cycle_rows(tickers, (now - timedelta(minutes=20)).timestamp(), now)
    return {t: r[-1] for t, r in rows.items() if r}


def market_books(tickers) -> dict:
    out: dict = {}
    tickers = list(tickers)
    if not tickers:
        return out
    try:
        sys.path.insert(0, pw.HERE)
        from kalshi_reads import kalshi_get_all
        for i in range(0, len(tickers), 50):
            chunk = tickers[i:i + 50]
            for m in kalshi_get_all("/markets", {"tickers": ",".join(chunk), "limit": len(chunk)},
                                    items_key="markets", max_pages=2):
                if m.get("ticker"):
                    out[m["ticker"]] = m
    except Exception as ex:  # noqa: BLE001
        print(f"! market read failed ({ex}); book reasons skipped", file=sys.stderr)
    return out


def _c(x):
    try:
        return round(float(x) * 100, 2) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def side_reason(t: str, sd: str, e: dict, ctx: dict) -> "tuple[str, str]":
    """(reason, kind) for side `sd` of listed market `t` not resting. kind: off / closed /
    held / hold / selection / guard / log / limit / queue / sized0 / unexplained."""
    wave_off = ctx.get("wave_off")
    if wave_off:
        return wave_off, "off"
    if e.get("bid_size") == 0 and e.get("ask_size") == 0:
        if isinstance(e.get("pre_off"), dict):
            return ("off on purpose: its multiple is 0 in the wave's size-mult file (both sides 0; only a closing "
                    "order may rest)"), "off"
        if ": 0 a side" in str(e.get("pinned", "")):
            return "off on purpose: pinned at 0 (both sides; only a closing order may rest)", "off"
    off = pw.dark_off_reason(e, None, {"waves": {}})
    if off:
        return off, "off"
    if e.get(f"{sd}_size") == 0:
        why = "pinned 0" if str(e.get("pinned", "")).startswith(tuple("0123456789")) else "sized 0"
        return (f"{sd} sized 0 on purpose ({why}: a side cap, the twin block, a 0 multiple or a pin 0; only a "
                f"closing order may rest)"), "off"
    st = (ctx["books"].get(t) or {}).get("status")
    if st and str(st).lower() not in ("active", "open"):
        return f"closed on Kalshi ({st}): nothing to quote", "closed"
    pos = float(ctx["pos"].get(t, 0.0) or 0.0)
    if abs(pos) >= 1:
        adding = (pos > 0 and sd == "bid") or (pos < 0 and sd == "ask")
        if adding:
            return f"held {pos:+,.0f}: only the closing side rests (the wave unwind)", "held"
        return (f"held {pos:+,.0f}: the closing order isn't resting (clamp / no-lead bound, or a 1-4 lot)"), "held"
    hold = pw.hold_for(t, ctx["holds"])
    if hold:
        return f"the bot's {hold[0]} hold (a breaker / halt; re-quotes when it ends)", "hold"
    dec = ctx["sel"].get(t)
    if dec and dec != "selected":
        return pw.SELECTION_WHY.get(dec, f"selection: {dec}"), "selection"
    g = ctx["guards"].get(t)
    if g:
        return pw.GUARD_WHY.get(g, f"the bot's {g} guard"), "guard"
    lr = ctx["log"].get((t, sd))
    if lr:
        return lr, "log"
    mk = ctx["books"].get(t) or {}
    yb, ya = _c(mk.get("yes_bid_dollars")), _c(mk.get("yes_ask_dollars"))
    try:
        bmax, amin = float(e.get("bid_max")), float(e.get("ask_min"))
    except (TypeError, ValueError):
        bmax = amin = None
    note = "the fair clamp" if e.get("fair_clamp") else "the capped re-anchor" if e.get("anchor_capped") \
        else "its limits"
    if sd == "bid" and bmax is not None and yb is not None and yb > bmax:
        return f"the best bid {yb:g}c is above its limit {bmax:g}c ({note}): it would lead the book", "limit"
    if sd == "ask" and amin is not None and ya is not None and 0 < ya < amin:
        return f"the best ask {ya:g}c is below its limit {amin:g}c ({note}): it would lead the book", "limit"
    cy = ctx["cycle"].get(t)
    if cy:
        wantn, own = (cy["wb"], cy["b"]) if sd == "bid" else (cy["wa"], cy["a"])
        if wantn > 0 and own <= 0:
            return "wanted, waiting in the placement queue (the cycle's placement cap)", "queue"
        if wantn <= 0:
            return "the bot sizes it 0 this cycle (full-earn cut, position room, caps or a rung guard)", "sized0"
    if dec == "selected" or dec is None:
        return "selected, nothing on record (just placed / just cancelled?)", "unexplained"
    return "unexplained", "unexplained"


def whydark_scan(names, now: "datetime | None" = None, ctx_over: "dict | None" = None) -> dict:
    now = now or datetime.now(timezone.utc)
    reg = pw.load()
    with open(pw.LIST, encoding="utf-8") as fh:
        L = json.load(fh).get("markets") or {}
    listed: dict = {}
    wave_of: dict = {}
    off_wave: dict = {}
    for n in names:
        w = reg["waves"].get(n)
        if not w:
            raise SystemExit(f"no wave {n}")
        for t, e in wave_tickers(n, w, L, now).items():
            listed[t] = e
            wave_of[t] = n
            if not w.get("enabled", True):
                off_wave[t] = f"its wave {n} is off"
    ctx = dict(ctx_over or {})
    if "resting" not in ctx:
        sys.path.insert(0, pw.HERE)
        from kalshi_reads import kalshi_get_all
        resting: dict = {}
        for o in kalshi_get_all("/portfolio/orders", {"status": "resting", "limit": 1000}, items_key="orders"):
            if o.get("ticker") in listed and float(o.get("remaining_count_fp") or o.get("remaining_count") or 0) >= 1:
                resting.setdefault(o["ticker"], set()).add(pw._order_side(o))
        ctx["resting"] = resting
    missing = [t for t in listed if len(ctx["resting"].get(t) or ()) < 2]
    if "pos" not in ctx or "holds" not in ctx:
        try:
            with open(pw.STATE_FILE, encoding="utf-8") as fh:
                st = json.load(fh) or {}
        except (OSError, ValueError):
            st = {}
        ctx.setdefault("pos", st.get("own_pos") or {})
        ctx.setdefault("holds", pw.active_holds(st, now.timestamp()))
    ctx.setdefault("sel", pw.latest_selection(missing, now) if missing else {})
    ctx.setdefault("guards", pw.latest_guard(missing, now) if missing else {})
    ctx.setdefault("log", log_side_reasons(missing, now) if missing else {})
    ctx.setdefault("books", market_books(missing) if missing else {})
    ctx.setdefault("cycle", latest_cycle(missing, now) if missing else {})
    res = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "waves": list(names), "listed": len(listed), "two_sided": 0,
           "one_sided": 0, "dark": 0, "by_reason": {}, "by_kind": {}, "by_series": {}, "rows": []}
    by_reason, by_kind = collections.Counter(), collections.Counter()
    by_series: dict = collections.defaultdict(collections.Counter)
    for t, e in sorted(listed.items()):
        sides = ctx["resting"].get(t) or set()
        res["two_sided" if len(sides) == 2 else "one_sided" if sides else "dark"] += 1
        for sd in ("bid", "ask"):
            if sd in sides:
                continue
            c2 = dict(ctx, wave_off=off_wave.get(t))
            why, kind = side_reason(t, sd, e, c2)
            by_reason[why if kind not in ("limit", "log", "held") else _generic(why, kind)] += 1
            by_kind[kind] += 1
            by_series[t.split("-")[0]][kind] += 1
            res["rows"].append({"ticker": t, "side": sd, "wave": wave_of[t], "reason": why, "kind": kind})
    res["by_reason"] = dict(by_reason.most_common())
    res["by_kind"] = dict(by_kind.most_common())
    res["by_series"] = {s: dict(c.most_common()) for s, c in sorted(by_series.items())}
    return res


def _generic(why: str, kind: str) -> str:
    if kind == "limit":
        return "the book is past the side's limit (" + ("fair clamp" if "fair clamp" in why else
                                                        "capped re-anchor" if "re-anchor" in why else "limits") + ")"
    if kind == "held":
        return why.split(":", 1)[1].strip() if ":" in why else why
    if kind == "log":
        return why.split(" (")[0]
    return why


def cmd_whydark(a) -> int:
    reg = pw.load()
    names = [n for n, w in reg["waves"].items() if w.get("enabled", True)] if a.name in (None, "all") else [a.name]
    res = whydark_scan(names)
    n_sides = sum(res["by_kind"].values())
    print(f"whydark {res['at']} ({', '.join(names)}): {res['listed']} listed live, {res['two_sided']} two-sided, "
          f"{res['one_sided']} one-sided, {res['dark']} dark; {n_sides} sides not resting")
    for why, n in res["by_reason"].items():
        print(f"  {n:6}  {why}")
    print("  by series (kind: sides):")
    for s, c in res["by_series"].items():
        print(f"    {s:22} " + ", ".join(f"{k} {v}" for k, v in c.items()))
    ex = collections.defaultdict(list)
    for r in res["rows"]:
        if len(ex[r["kind"]]) < 2:
            ex[r["kind"]].append(f"{r['ticker']} {r['side']}: {r['reason']}")
    for k, xs in ex.items():
        for x in xs:
            print(f"    e.g. [{k}] {x}")
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(res, fh, indent=1)
    return 0
