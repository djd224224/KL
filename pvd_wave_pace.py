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
  * TRIM, on a wave whose floor "trim" is on (`set NAME floor_trim on`; Jack 2026-11-18 on
    the FX relight: "dont overquote if unneeded to hit the target threshold"): a market
    that needs less size for its target comes DOWN to that size. The target: a projection
    of TRIM_TO x the aim ($1.50 on $1.20) and TRIM_MIN_SHARE of the pool (2.25%; Jack
    11/18: "at least 2% share of each book"), modelled with the WHOLE share loss counted
    (others standing still) -- which still OVER-CUT on wall books: on FX 11/19 a 4x cut lost
    ~10x the share (7.7% -> 0.7%), 74 of 186 strikes fell under 2%, and it went off (Jack:
    "Leave it off"). Rebuild it as step-and-measure before turning it on again. Only on two reads at the same
    multiple, only a cut of 1 - TRIM_TRIGGER (20%) or more, never under TRIM_MIN_MULT, and
    never under TRIM_MAX_CUT of the multiple in one step (the next reads measure the new
    size before cutting again). Each trimmed market's earlier multiple waits in
    trim_NAME.json and comes back when its period ends (a relight), when it leaves the list,
    or when the wave's trim goes off; a size-up back to it drops the record. The $1.50 lands
    over the $1.20 that sizes a market up, so the two don't flap. Off by default: the crypto
    wave keeps 1,500 a side (Jack 11/15) and the default tune's --no-drop (11/07: "dont
    drop any size after banking 1.3") stands elsewhere;
  * TIER REACH, on a wave whose floor "tier_to" is set (`set NAME floor_tier 25`; Jack
    2026-11-19, the FX walls having stepped to 16-25c past the 15c tier: "Up to 25c on cheap
    tier multiplier if needed"): the LAST cheap tier's multiple (FX: x5) on a side costing
    past that tier up to tier_to c a contract (never past TIER_REACH_MAX, 25c), for one
    market at a time, ONLY where it is needed -- the multiple goes first (a quarter step
    raises the cheap side too, the cheaper share), and a market gets the reach when its
    multiple can't take it to the aim: at the cap still short of the aim, or under $1 even
    at the cap (instead of going off). A market the check turned off whose side now sits in
    the reach comes back at 1x with it (the 11/19 FX offs: deep in-the-money MXN strikes
    whose asks the fair keeps out and whose bids sat at 20-25c walls), and the next reads
    size it like any other. One reach per market a period: one still under $1 at the cap
    with it goes off on two reads as before and stays off. The reaches live in
    tier_NAME.json (the tune's --tier-file); a relight or leaving the list drops a market's,
    and `floor_tier off` drops them all at the next tune. A blanket 25c tier would have
    put ~$415k more on the FX books (5x the 463 sides at 16-25c, 11/19 12:00Z) while 305 of
    305 judged strikes already projected $1+;
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

import argparse
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
# TRIM (the module docstring; Jack 2026-11-18: "dont overquote if unneeded to hit the target
# threshold")
TRIM_TO = 1.25             # a trim keeps the projection at 1.25 x the aim ($1.50 on $1.20)
TRIM_MIN_SHARE = 0.0225    # ...and 2.25% of the pool (Jack 11/18: "at least 2% share of each book")
TRIM_TRIGGER = 0.8         # only a cut to 80% of the multiple or less (no churn on noise)
TRIM_MIN_MULT = 0.2        # the smallest multiple a trim leaves (a 5x cheap side at its base)
TRIM_MAX_CUT = 0.25        # one step leaves at least a quarter of the multiple
TRIM_GRID = 0.05           # a trimmed multiple rounds UP to this step
# TIER REACH (the module docstring; Jack 2026-11-19: "Up to 25c on cheap tier multiplier if needed")
TIER_REACH_MAX = 25.0      # no market's reach past 25c a contract (Jack's cap)


def size_mult_path(name: str) -> str:
    return os.path.join(pw.HERE, "run-logs", "pvd-scan", f"size_mult_{name}.json")


def trim_state_path(name: str) -> str:
    """{ticker: {"from": the multiple before its trims, "to", "at", "stop", "period_start"}}"""
    return os.path.join(pw.HERE, "run-logs", "pvd-scan", f"trim_{name}.json")


def resize_state_path(name: str) -> str:
    return os.path.join(pw.HERE, "run-logs", "pvd-scan", f"resize_state_{name}.json")


def tier_state_path(name: str) -> str:
    """{"to": C, "markets": {ticker: {"at", "why", "sides", "stop", "period_start"}}} -- the
    tune's --tier-file (TIER REACH)"""
    return os.path.join(pw.HERE, "run-logs", "pvd-scan", f"tier_{name}.json")


def last_tier(tune) -> "tuple | None":
    """(C, X) of the tune's last --cheap-tiers step (the highest C), or None."""
    specs = pw._vals(tune or [], "--cheap-tiers")
    best = None
    for part in (specs[-1] if specs else "").split(","):
        try:
            c, x = (float(v) for v in part.split(":"))
        except ValueError:
            continue
        if best is None or c > best[0]:
            best = (c, x)
    return best


def reach_sides(costs, lo: float, hi: float) -> list:
    """The sides costing past the last tier (lo) and within the reach (hi) c a contract."""
    out = []
    for k, c in sorted((costs or {}).items()):
        try:
            if lo < float(c) <= hi:
                out.append(k)
        except (TypeError, ValueError):
            continue
    return out


def load_reach(name: str) -> dict:
    """{ticker: record} from tier_NAME.json ({} when missing or unreadable)."""
    try:
        with open(tier_state_path(name), encoding="utf-8") as fh:
            d = json.load(fh) or {}
        return {str(k): dict(v) for k, v in (d.get("markets") or {}).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def wave_reach(name: str) -> tuple:
    """reach_of() for a registry wave by name ((0, None) when unreadable)."""
    try:
        w = pw.load()["waves"].get(name) or {}
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0, None
    return reach_of(w)


def reach_of(w: dict) -> tuple:
    """(tier_to, the last tier's C) for a wave's TIER REACH: tier_to is 0 unless its floor
    "tier_to" is set past its last tier and its tune reads the --tier-file; never past
    TIER_REACH_MAX."""
    tune = w.get("tune") or []
    lt = last_tier(tune)
    fl = w.get("floor") if isinstance(w.get("floor"), dict) else {}
    to = min(TIER_REACH_MAX, float(fl.get("tier_to") or 0))
    if not pw._vals(tune, "--tier-file") or lt is None or to <= lt[0]:
        to = 0.0
    return to, (lt[0] if lt else None)


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
                                         "rate": f("est_frac") * pool, "pool": pool,
                                         "hm": (f("hour_mult") or 1.0) if "hour_mult" in ix else 1.0})
    return out


def held_at_base(r: dict, lst) -> bool:
    """The bot wants less than the list asks for on a side it quotes at all: its first
    hour on a market (the off-usual rule's cold start, under 4 touch samples 15 min
    apart, rests the entry's base size -- not the cheap tiers or the multiple) or its
    off-usual cut. The share it measures is then a fraction of the listed size, so the
    multiple that size would need can't be judged from it (2026-11-18, w1113-fxmetals:
    the 19:33Z and 19:48Z reads judged 99 new FX strikes at the 300 a side the bot
    rested in its first hour, against 1,500 listed -- 5x of the listed size is 7,500 --
    and turned them all off; one it sized up instead went 0.48% -> 2.35% at 1,500).
    `lst` = (bid, ask) contracts the list asks for; the row's hour multiple scales it."""
    if not lst:
        return False
    hm = r.get("hm") or 1.0
    return any(0 < w < LANDED_FRAC * n * hm for w, n in ((r["wb"], lst[0]), (r["wa"], lst[1])))


def judge_rows(rows: list, lst=None) -> dict:
    """The market's measured state from its rows (oldest first): status, the average rate
    (resting-weighted), uptime, share while resting, the latest own / wanted contracts.
    `lst` = (bid, ask) contracts the list asks for: a row where the bot holds a side under
    it is "held" (held_at_base), and only rows after the latest such row count."""
    if not rows:
        return {"status": "no rows"}
    last = rows[-1]
    own_l, want_l = last["b"] + last["a"], last["wb"] + last["wa"]
    # LANDING = resting some, but under LANDED_FRAC of what the bot wants (a size step not
    # rested yet). Resting nothing is not landing: that cycle simply earns 0 (a stand-down,
    # a queued side, a breaker) and lowers the uptime.
    landing = lambda o, wn: wn > 0 and 0 < o < LANDED_FRAC * wn
    if held_at_base(last, lst):
        return {"status": "held", "own": own_l, "want": want_l, "list": sum(lst)}
    if landing(own_l, want_l):
        return {"status": "landing", "own": own_l, "want": want_l}
    i0 = 0
    for i in range(len(rows) - 1, -1, -1):
        if landing(rows[i]["b"] + rows[i]["a"], rows[i]["wb"] + rows[i]["wa"]) or held_at_base(rows[i], lst):
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


def share_at(r: float, s: float) -> float:
    """Our share with our size x r, others standing still (the module docstring's model)."""
    s = min(max(s or 0.0, 0.0), 0.999)
    return r * s / (1 + (r - 1) * s) if s > 0 else 0.0


def trim_mult(j: dict, banked: float, hrs: float, m0: float, aim: float):
    """TRIM: the smaller multiple a market over its target needs, or None. The smallest
    TRIM_GRID step from max(TRIM_MIN_MULT, TRIM_MAX_CUT x m0) up to m0 that still keeps
    TRIM_MIN_SHARE of the pool and projects TRIM_TO x the aim -- the whole share loss
    counted (real = 1) -- and only if it is TRIM_TRIGGER x m0 or less."""
    m0 = m0 or 1.0
    s = j.get("share") or 0.0
    if s <= 0 or m0 <= TRIM_MIN_MULT + 1e-9:
        return None
    target = TRIM_TO * aim

    def ok(x: float) -> bool:
        r = x / m0
        return share_at(r, s) >= TRIM_MIN_SHARE and proj_at(r, j, banked, hrs, 1.0) >= target

    k_lo = math.ceil(max(TRIM_MIN_MULT, TRIM_MAX_CUT * m0) / TRIM_GRID - 1e-9)
    k_hi = math.floor(TRIM_TRIGGER * m0 / TRIM_GRID + 1e-9)       # a cut of 20%+ or none
    if k_hi < k_lo or not ok(k_hi * TRIM_GRID):
        return None
    # ok() rises with the multiple: the smallest step that passes
    if ok(k_lo * TRIM_GRID):
        return round(k_lo * TRIM_GRID, 2)
    lo, hi = k_lo, k_hi                 # ok(hi), not ok(lo)
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ok(mid * TRIM_GRID):
            hi = mid
        else:
            lo = mid
    return round(hi * TRIM_GRID, 2)


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
        e = listed[t]
        lst = tuple(float(e.get(k, e.get("size")) or 0) for k in ("bid_size", "ask_size"))
        # only the cycles since the entry's last size change on the list (the tune's
        # "scaled_at" / "unpinned" stamps): earlier ones measured another size -- or none
        # (2026-11-18 20:16:39Z: the 99 FX strikes back from a 0 were judged on the half
        # hour they rested nothing while pinned off, 3 s before the tune brought them back)
        chg = max(str(e.get("scaled_at") or "")[:19], str(e.get("unpinned") or "")[:19])
        j = judge_rows([r for r in rows.get(t) or [] if not chg or r["ts"][:19] >= chg], lst)
        if m0 != 0 and isinstance(e.get("pre_off"), dict):
            # the multiples file lifted its 0, the tune hasn't yet: nothing to judge
            j = {"status": "restoring"}
        banked = float(dr.get("period_est") or 0.0)
        hrs = float(dr.get("hrs_left") if dr.get("hrs_left") is not None else
                    max(0.0, ((_ts(listed[t].get("stop")) or nts) - nts) / 3600))
        m = {"ticker": t, "series": t.split("-")[0], "m0": m0, "banked": round(banked, 3), "hrs": round(hrs, 2),
             "status": "off" if m0 == 0 else j["status"], "costs": side_costs(dr),
             "stop": e.get("stop"), "period_start": e.get("period_start")}
        if m0 == 0:
            # its sizes wait in "pre_off" (the tune's): only then can a 0 lifted bring it back
            m["pre_off"] = isinstance(e.get("pre_off"), dict)
        if m["status"] == "judged":
            proj = proj_at(1.0, j, banked, hrs, 0.0)
            x, p_max = needed_mult(j, banked, hrs, m0, a.aim, res["max_mult"], res["real"])
            m.update(proj=round(proj, 3), rate=round(j["rate"], 4), share=round(j["share"], 4),
                     uptime=round(j["uptime"], 3), pool=round(j["pool"], 3), two_sided=j["two_sided"],
                     own_b=j["own_b"], own_a=j["own_a"], x_aim=x, proj_max=round(p_max, 3),
                     x_trim=trim_mult(j, banked, hrs, m0, a.aim))
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
          f"yet), {st.get('held', 0)} held under the listed size by the bot (its first hour on a market, or its "
          f"off-usual cut: not judged), {st.get('not resting', 0)} not resting, {st.get('no rows', 0)} with no bot "
          f"rows, {st.get('off', 0)} off (multiple 0)"
          + (f", {st['restoring']} back from a 0 the tune hasn't applied yet" if st.get("restoring") else ""))
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
    """{"up": {t: x}, "off": [t], "off_next": [t], "down": {t: x}, "down_next": [t], "keep": n,
    "reach": {t: why}, "reach_restore": {t: x}, ...} from measure()'s result and the previous
    resize read (the two-read rule; a trim's two reads must see the same multiple). Trims only
    with `a.trim`; TIER REACH only with `a.reach_to` past `a.reach_lo` (the last tier's C), never
    twice for a market (`a.reach_granted`)."""
    nts = now.timestamp()
    prev_at = _ts(prev.get("at"))
    gap = (nts - prev_at) / 60 if prev_at else None
    prev_ok = gap is not None and TWO_READ_GAP_MIN[0] <= gap <= TWO_READ_GAP_MIN[1]
    prev_off = set(prev.get("off_candidates") or []) if prev_ok else set()
    prev_trim = dict(prev.get("trim_candidates") or {}) if prev_ok else {}
    trim = bool(getattr(a, "trim", False))
    reach_to = float(getattr(a, "reach_to", 0) or 0)
    reach_lo = getattr(a, "reach_lo", None)
    granted = set(getattr(a, "reach_granted", None) or ())
    reach_on = reach_lo is not None and reach_lo < reach_to <= TIER_REACH_MAX

    def reach(m) -> list:
        """The market's sides the reach would size, when it may get one."""
        if not reach_on or m["ticker"] in granted:
            return []
        return reach_sides(m.get("costs"), reach_lo, reach_to)
    out = {"up": {}, "off": [], "off_next": [], "down": {}, "down_next": [], "keep": 0, "at_max": 0, "borderline": 0,
           "skipped": collections.Counter(), "prev_gap_min": round(gap, 1) if gap is not None else None,
           "candidates": [], "trim_candidates": {}, "reach": {}, "reach_restore": {}}
    for m in res["markets"]:
        if m["status"] == "off" and m.get("pre_off") and reach(m):
            # off for being under $1 at the cap -- with a side now inside the reach: back at
            # 1x with it, sized from there like any other market
            out["reach_restore"][m["ticker"]] = 1.0
            continue
        if m["status"] != "judged":
            out["skipped"][m["status"]] += 1
            continue
        if m["proj"] >= a.aim:
            xt, m0 = m.get("x_trim"), m["m0"] or 1.0
            if trim and xt is not None and xt < m0 - 1e-9:
                out["trim_candidates"][m["ticker"]] = m0
                pm = prev_trim.get(m["ticker"])
                if pm is not None and abs(float(pm) - m0) < 1e-9:
                    out["down"][m["ticker"]] = xt               # over its target on two reads at this size
                else:
                    out["down_next"].append(m["ticker"])
                continue
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
            elif reach(m):
                # at the cap, short of the aim: the multiple has nothing left -- the reach
                out["reach"][m["ticker"]] = f"at the {m0:g}x cap, short of the ${a.aim:.2f} aim"
            else:
                out["at_max"] += 1
            continue
        if reach(m):
            # under $1 even at the cap: the reach before an off (the next reads judge it with it)
            out["reach"][m["ticker"]] = f"under $1 even at {a.max_mult:g}x (${m['proj_max']:.2f})"
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


def wave_trim_on(name: str) -> bool:
    """The wave's floor "trim" switch (`set NAME floor_trim on|off`); off when unreadable."""
    try:
        w = pw.load()["waves"].get(name) or {}
    except (OSError, ValueError, KeyError, TypeError):
        return False
    fl = w.get("floor") if isinstance(w.get("floor"), dict) else {}
    return bool(fl.get("trim"))


def apply_trims(name: str, res: dict, plan: dict, cur: dict, trim_on: bool) -> tuple:
    """TRIM into `cur` (the multiples, in place): plan["down"]'s lower multiples, each one's
    earlier multiple kept in trim_NAME.json; that multiple comes back when the trim is off,
    when the market's period changed (its stop / period_start: a relight) or it left the
    list; a record a size-up reached is dropped; an off (0) stays off. Returns (trimmed,
    given back)."""
    tp = trim_state_path(name)
    try:
        with open(tp, encoding="utf-8") as fh:
            rec = {str(k): dict(v) for k, v in (json.load(fh) or {}).items()}
    except (OSError, ValueError, TypeError, AttributeError):
        rec = {}
    if not rec and not (trim_on and plan.get("down")):
        return 0, 0
    live = {m["ticker"]: m for m in res.get("markets") or []}
    n_down = n_back = 0
    for t, r in list(rec.items()):
        m, c, f = live.get(t), cur.get(t, 1.0), float(r.get("from") or 1.0)
        if c == 0:
            del rec[t]                                  # off: stays off (its pre_off keeps the sizes)
            continue
        moved = m is None or m.get("stop") != r.get("stop") or m.get("period_start") != r.get("period_start")
        if not trim_on or moved:
            if c < f - 1e-9:
                cur[t] = f
                n_back += 1
            del rec[t]
        elif c >= f - 1e-9:
            del rec[t]                                  # a size-up reached it
    if trim_on:
        for t, x in (plan.get("down") or {}).items():
            c = cur.get(t, 1.0)
            if c == 0 or x >= c - 1e-9:
                continue
            m, r = live.get(t) or {}, rec.get(t)
            rec[t] = {"from": float(r["from"]) if r else c, "to": x, "at": res.get("at"),
                      "stop": m.get("stop"), "period_start": m.get("period_start")}
            cur[t] = x
            n_down += 1
    os.makedirs(os.path.dirname(tp), exist_ok=True)
    with open(tp + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=0, sort_keys=True)
    os.replace(tp + ".tmp", tp)
    return n_down, n_back


def apply_reach(name: str, res: dict, plan: dict, cur: dict, reach_to: float, reach_lo) -> tuple:
    """TIER REACH into tier_NAME.json (the tune's --tier-file) and `cur` (the multiples, in
    place): plan["reach"]'s markets get a record; plan["reach_restore"]'s come back from 0 at
    their multiple with one. A record goes when the reach is off (reach_to 0: every one),
    when its market's period changed (its stop / period_start: a relight) or it left the
    list; one whose market went off again stays, so it isn't brought back twice. The file's
    "to" is the reach the tune applies now. Returns (given, restored, dropped)."""
    tp = tier_state_path(name)
    rec = load_reach(name)
    if not rec and not os.path.exists(tp) and not (reach_to and (plan.get("reach") or plan.get("reach_restore"))):
        return 0, 0, 0
    live = {m["ticker"]: m for m in res.get("markets") or []}
    n_drop = 0
    for t, r in list(rec.items()):
        m = live.get(t)
        moved = m is None or m.get("stop") != r.get("stop") or m.get("period_start") != r.get("period_start")
        if not reach_to or moved:
            del rec[t]
            n_drop += 1
    n_give = n_rest = 0

    def record(t: str, why: str) -> dict:
        m = live.get(t) or {}
        return {"at": res.get("at"), "why": why, "sides": reach_sides(m.get("costs"), reach_lo or 0.0, reach_to),
                "stop": m.get("stop"), "period_start": m.get("period_start")}
    if reach_to:
        for t, why in (plan.get("reach") or {}).items():
            if t in rec or cur.get(t, 1.0) == 0:
                continue
            rec[t] = record(t, why)
            n_give += 1
        for t, x in (plan.get("reach_restore") or {}).items():
            if t in rec or cur.get(t, 1.0) != 0:
                continue
            cur[t] = x
            rec[t] = dict(record(t, "back from off with the reach"), restored=x)
            n_rest += 1
    os.makedirs(os.path.dirname(tp), exist_ok=True)
    with open(tp + ".tmp", "w", encoding="utf-8") as fh:
        json.dump({"to": reach_to, "markets": rec}, fh, indent=0, sort_keys=True)
    os.replace(tp + ".tmp", tp)
    return n_give, n_rest, n_drop


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
    if getattr(a, "trim", None) is None:
        a.trim = wave_trim_on(a.name)
    if getattr(a, "reach_to", None) is None:
        a.reach_to, a.reach_lo = wave_reach(a.name)
    a.reach_granted = set(load_reach(a.name))
    plan = plan_resize(res, prev, a, now)
    print(f"\nresize plan (aim ${a.aim:.2f}, up to {a.max_mult:g}x, {a.real:.0%} of the modelled gain): "
          f"{len(plan['up'])} up, {len(plan['off'])} off now (under $1 at {a.max_mult:g}x on two reads), "
          f"{len(plan['off_next'])} off at the next read if still short, {plan['keep']} keep, "
          f"{plan['borderline']} borderline kept, {plan['at_max']} already at the cap; skipped "
          f"{dict(plan['skipped'])}; previous read {plan['prev_gap_min']} min ago")
    if a.trim:
        print(f"  trim (${TRIM_TO * a.aim:.2f} and {TRIM_MIN_SHARE:.2%} share, whole loss counted): "
              f"{len(plan['down'])} down now (over it on two reads at the same size), "
              f"{len(plan['down_next'])} down at the next read if still over")
    if a.reach_to:
        print(f"  tier reach to {a.reach_to:g}c (a side past the {a.reach_lo:g}c tier, where the multiple can't "
              f"reach the aim): {len(plan['reach'])} given, {len(plan['reach_restore'])} back from off at 1x; "
              f"{len(a.reach_granted)} held one already")
    off_only = bool(getattr(a, "off_only", False))
    if off_only:
        print(f"  OFF-ONLY (no live fair): {len(plan['up'])} size-up(s) not applied; a market pacing under "
              f"${1.0 - a.keep_within:.2f} that can't reach $1 goes off on its second read")
    by = collections.Counter(round(x, 2) for x in plan["up"].values())
    if by and not off_only:
        print("  new multiples: " + ", ".join(f"{k:g}x {v}" for k, v in sorted(by.items())))
    # the read is recorded for the two-read rule even on a dry run (a read is a read)
    if not low or a.force:
        with open(sp + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"at": res["at"], "off_candidates": plan["candidates"],
                       "trim_candidates": plan["trim_candidates"], "applied": bool(a.apply)}, fh, indent=1)
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
    for t, x in ({} if off_only else plan["up"]).items():
        if cur.get(t, 1.0) != 0 and x > cur.get(t, 1.0) + 1e-9:     # never lowers; an off stays off
            cur[t] = x
            n_up += 1
    for t in plan["off"]:
        if cur.get(t, 1.0) != 0:
            cur[t] = 0
            n_off += 1
    if off_only:                     # offs only: no trims, trim give-backs or tier reach
        n_down = n_back = n_reach = n_rest = n_rdrop = 0
    else:
        n_down, n_back = apply_trims(a.name, res, plan, cur, bool(a.trim))
        n_reach, n_rest, n_rdrop = apply_reach(a.name, res, plan, cur, float(a.reach_to or 0), a.reach_lo)
    os.makedirs(os.path.dirname(mp), exist_ok=True)
    with open(mp + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=0, sort_keys=True)
    os.replace(mp + ".tmp", mp)
    print(f"WROTE {mp}: {n_up} up, {n_off} off, {n_down} trimmed, {n_back} trim(s) given back "
          f"({sum(1 for v in cur.values() if v > 0)} sized, {sum(1 for v in cur.values() if v == 0)} off in the file)"
          + (f"; tier reach: {n_reach} given, {n_rest} back from off, {n_rdrop} dropped"
             if a.reach_to or n_rdrop else ""))
    if getattr(a, "no_run", False):
        # pass_resize: the 15-min pass holds this wave's lock and tunes it next, so no setarg
        # (it would wait on that lock) and no second run
        print("    (this pass's tune applies them)")
        return 0
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


FLOOR_MAX_MULT = 5.0       # never above 5x (Jack 2026-11-14: "go up to 5x every market")

# OFF-ONLY FLOOR CHECK (review R-261124-08, Jack 2026-11-25: "Yes, all four"): a wave with
# no live fair still gets the check when its floor max_mult is 1 (`set NAME floor_max_mult 1`)
# -- a fair is needed only to SIZE UP. It then only turns markets off: no size-ups, trims,
# trim give-backs or tier reach. It keeps a market pacing within OFF_ONLY_KEEP_WITHIN of $1
# (the floor's "keep_within" overrides). Wave entries force their whole event past the bot's
# payout floor and hopeless exit, so without this a long-shot strike rides to the period end:
# 134 of the 2028 election wave's markets closed 11/24 under $0.90, and the forced misses lost
# -$148 of trading over 11/18-24. Backtest on that period: off under $0.80 at 11/20 06:00Z cut
# 62 of 125 misses, avoided -$92 and cut no market that cleared; at $0.95 it cut 16 that did.
OFF_ONLY_KEEP_WITHIN = 0.20


# THE BOT'S OWN LIVE FAIRS (Jack 2026-11-15: "build these to generalize into the market-wave
# skill"): a wave whose fair lives in the bot, not in a fair file -- the crypto wave's
# (crypto_event_fair) -- counts as live while one of these status files lists its series'
# strikes and was written within BOT_FAIR_FRESH_MIN.
BOT_FAIRS = ("crypto_event_fair.json", "fx_event_fair.json")     # in the bot's status dir
BOT_FAIR_FRESH_MIN = 5.0


def _bot_fair_live(w: dict, now: datetime) -> "str | None":
    """None when the bot prices the wave's series live (BOT_FAIRS), else why not."""
    mine = set(pw.all_series(w))
    for fn in BOT_FAIRS:
        try:
            with open(os.path.join(os.path.dirname(pw.LIST), fn), encoding="utf-8") as fh:
                d = json.load(fh) or {}
        except (OSError, ValueError):
            continue
        if not mine & {str(t).split("-")[0] for t in (d.get("markets") or {})}:
            continue
        age = now.timestamp() - float(d.get("checked_ts") or 0.0)
        return None if age <= BOT_FAIR_FRESH_MIN * 60 else f"the bot's {fn} is {age / 60:.0f} min old"
    return "no --fair-file in its tune, and the bot prices none of its series live"


def _fair_live(w: dict, now: datetime) -> "str | None":
    """None when the wave has a live fair -- its --fair-file fresh (its valid_until ahead),
    or with no fair file the bot's own live fair for its series (_bot_fair_live) -- else why
    not."""
    ff = pw._vals(w.get("tune") or [], "--fair-file")
    if not ff:
        return _bot_fair_live(w, now)
    try:
        with open(ff[-1], encoding="utf-8") as fh:
            vu = _ts((json.load(fh) or {}).get("valid_until"))
    except (OSError, ValueError) as e:
        return f"fair file unreadable ({e.__class__.__name__})"
    if vu is None or vu <= now.timestamp():
        return "fair file stale"
    return None


def pass_resize(name: str, w: dict, now: "datetime | None" = None) -> int:
    """THE $1-FLOOR CHECK IN EVERY 15-MIN PASS, for a wave with "floor_auto": true (Jack
    2026-11-14 on w1113-fxmetals: "Yes, every 15 min", to "Run this two-read check
    automatically in the wave's 15-minute pass until the stop?"). = `resize NAME --apply`
    with the defaults, minus its own setarg and wave run: the pass calls this right before
    the wave's tune, which reads the multiples file. Two reads as by hand: a market goes off
    only when the previous read, 5-90 min earlier (the last pass), judged it the same way.
    Skips -- and changes nothing -- unless the wave's tune has --size-mult-file and a fresh
    --fair-file (the floor protocol needs a live fair to size up); with no live fair and the
    floor's max_mult at 1 it runs OFF-ONLY (OFF_ONLY_KEEP_WITHIN: offs only, kept within
    $0.20 of $1). cmd_resize itself skips within 15 min of a bot restart and on a stale
    dashboard read. Never above 5x; an off stays off.
    With the wave's floor "trim" on, a market over its target comes down to it (TRIM in the
    module docstring); with it off, the trims it made are given back. With its floor
    "tier_to" set (and --tier-file in its tune), a market the multiple can't take to the aim
    gets the last cheap tier out to tier_to c (TIER REACH)."""
    now = now or datetime.now(timezone.utc)
    tune = w.get("tune") or []
    if not pw._vals(tune, "--size-mult-file"):
        print("    skipped: no --size-mult-file in its tune (`set NAME floor_auto on` adds it)")
        return 0
    fl = w.get("floor") if isinstance(w.get("floor"), dict) else {}
    max_mult = min(FLOOR_MAX_MULT, float(fl.get("max_mult", FLOOR_MAX_MULT)))
    why = _fair_live(w, now)
    off_only = False
    if why:
        if max_mult > 1.0 + 1e-9:
            print(f"    skipped: {why} (the floor protocol needs a live fair to size up; "
                  f"`set NAME floor_max_mult 1` runs it off-only)")
            return 0
        off_only = True
        print(f"    off-only: {why}, and the floor's max_mult is 1 -- the check only turns markets off")
    keep = fl.get("keep_within")
    keep_within = ((OFF_ONLY_KEEP_WITHIN if off_only else 0.05) if keep is None
                   else min(max(float(keep), 0.0), 0.5))
    reach_to, reach_lo = (0.0, None) if off_only else reach_of(w)
    a = argparse.Namespace(name=name, aim=float(fl.get("aim", 1.20)), max_mult=max_mult,
                           real=0.65, window_min=60.0, keep_within=keep_within, no_off=False, json=None,
                           apply=True, force=False, no_run=True, trim=bool(fl.get("trim")) and not off_only,
                           reach_to=reach_to, reach_lo=reach_lo, off_only=off_only)
    return cmd_resize(a)


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
_RX_FX = re.compile(r"\] fx-event stand-aside (\S+): (.*)$")
_RX_FX_RES = re.compile(r"\] fx-event resume (\S+)")


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
            continue
        m = _RX_FX.search(ln)
        if m and m.group(1) in want:
            for sd in ("bid", "ask"):
                out[(m.group(1), sd)] = f"FX fair stands it aside ({m.group(2)[:70]})"
            continue
        m = _RX_FX_RES.search(ln)
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
