#!/usr/bin/env python3
r"""send_lone_ref_monitor.py -- the daily lone-reference guard email (Jack
2026-10-16: "turn it on, and track the results").

THE GUARD (ae57d2b, on since 16:17Z 10/16; incentive_mm LONE_REF_GUARD_MODE):
a side whose reward reference rests on a thin external top, far above the
rest of the book, that our rung would jump to, stands aside while the thin
top lasts. The 9/30-10/16 replay put it at ~$21/day of reward given up for
~$48/day of fills avoided (net ~+$27/day, mention books out).

ONE EMAIL per ET day, ending in OK / WATCH / PROBLEM:
  holds    how often the guard held a side, for how long, how many sides at
           once, which sides flapped; AT the touch (the bait shape: the
           reference within 2c of the external best) or INSIDE the book (a
           ladder of small orders above the reference)
  cost     reward given up: on every managed book snapshot of a held side a
           rung of our pre-hold size is put back at the reference and scored
           with the bot's own share model (incentive_mm._side_share)
  avoided  every public Kalshi trade that went through a held side's price
           during the hold, our would-be fill capped at that pre-hold size,
           marked to the current mid
  let thru full-rung maker fills whose rung jumped >= the guard's gap within
           its jump window before the fill, the shape the guard stops, marked
           to the current mid; unguarded series (mention books, and since
           10/18 fair-gated markets) listed apart
  running  totals since the guard went live, against the replay's +$27/day;
           since 10/18 (fair-gated markets out) the verdict judges the markets
           it still guards, against the replay's +$17/day there

SOURCES, all read-only: the bot's day logs (incentive-mm-*.log), the
book_depth snapshots, fills_ / orders_<UTC date>.jsonl, the dashboard data
object (family names) and Kalshi's PUBLIC GET /markets and /markets/trades
(no auth, paced). No orders. The only files written are the sent-marker and
lone_ref_monitor.json (each day's result, for the running totals). Imports
imm_quote_gaps first, like the other morning emails, so the launcher env
and the alert credentials apply. Runs at idle priority: the bot shares the PC.

USAGE:
  python send_lone_ref_monitor.py                   # the scheduled daily run
  python send_lone_ref_monitor.py --dry             # build + print, send nothing
  python send_lone_ref_monitor.py --test            # send now, ignore the marker
  python send_lone_ref_monitor.py --day 2026-10-16  # a specific ET day
  python send_lone_ref_monitor.py --no-tape         # skip the Kalshi pulls
Scheduled daily as "KL imm lone-ref" (register_imm_lone_ref.ps1), 07:50 ET.
Sends every morning, so a silent day is distinguishable from a broken task.
All console output is ASCII (the task console is cp1252).
"""
import argparse
import ctypes
import glob
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_quote_gaps as _gaps  # noqa: F401,E402  (import side effects)
import incentive_mm as imm  # noqa: E402
from incentive_mm import log  # noqa: E402

GUARD_LIVE_TS = datetime(2026, 10, 16, 16, 17, tzinfo=timezone.utc).timestamp()
REPLAY_NET_PER_DAY = 27.0       # the replay's net, $/day (mention books out)
AT_TOUCH_C = 2                  # reference within this of the external best = at the touch
DT_CAP_SECS = 180.0             # a book snapshot stands for at most this long
FALLBACK_SIZE = 60.0            # rung size when no pre-hold size is known
LOOKBACK_SECS = 6 * 3600        # day-log read starts this far before the window
# verdict thresholds (dollars; a partial day scales the cost bars)
LET_THRU_PROBLEM = -100.0       # guarded jumped fills marked to now
LET_THRU_WATCH = -25.0
COST_PROBLEM_PER_DAY = 60.0
COST_WATCH_PER_DAY = 35.0
DAY_NET_WATCH = -10.0
RUN_NET_PROBLEM = -50.0         # running net once RUN_MIN_DAYS have passed
RUN_MIN_DAYS = 3
# FAIR VALUE (Jack 2026-10-17: "Don't we have fair value on rain though? So
# shouldn't this guard apply where we don't have fair value" -> "Keep as is
# but monitor in daily emails"). The replay (9/30-10/16, mentions out) had
# the guard at +$9.56/day on fair-gated markets and +$17.44 on the rest:
# fair gates leak (OpenRouter share, RT first reviews, weekend rain's wide
# band). Only Carbon Arc and Vercel lost money, ~-$1.8/day each. The email
# splits every day and the running totals by gate, names these, and WATCHes
# a fair-gated family whose running net reaches FAIR_GATE_WATCH.
FAIR_GATE_WATCH = -25.0
# FAIR-GATED MARKETS LEFT OUT (Jack 2026-10-18: "Build it", to "Take
# fair-gated markets out of the lone-ref guard?"; incentive_mm
# LONE_REF_SKIP_FAIR). Live 10/16-17 the guard netted -$78, all fair-gated
# (weekend rain -$66, quake -$19); markets without a fair gate +$7.5. While
# the bot skips them, the running verdict judges the markets the guard still
# covers -- the "none" gate, against the replay's +$17.44/day there -- and no
# fair-gated family is flagged to leave out (it is out).
REPLAY_NO_FAIR_NET_PER_DAY = 17.44
FAIR_NAMED = ("carbon arc", "vercel", "rain weekend")
REPLAY_GATE_NET_PER_DAY = {"carbon arc": -1.8, "vercel": -1.8, "rain weekend": 2.7}
FAIR_NAMED_LABEL = {"carbon arc": "Carbon Arc", "vercel": "Vercel", "rain weekend": "Weekend rain"}
FLAP_WATCH = 200                # one side held this many times in the day (a storm;
                                # the inside-the-book ladders flap ~100/day on their own)
KALSHI = os.environ.get("IMM_LONE_REF_MONITOR_KALSHI",
                        "https://api.elections.kalshi.com/trade-api/v2")
TAPE_GAP_SECS = 0.5
TAPE_BUDGET_SECS = 900
LEVELS = ("OK", "WATCH", "PROBLEM")


def store_path() -> str:
    """Resolved at call time, so a sandboxed STATUS_DIR (tests) is honoured."""
    return os.path.join(imm.STATUS_DIR, "lone_ref_monitor.json")


def dash_path() -> str:
    return os.path.join(imm.STATUS_DIR, "dashboard", "imm_dashboard.html")

_HOLD = re.compile(
    r"^(\S+ \S+)Z \[IMM\] lone-ref guard (?:\(dry\) )?(\S+) (bid|ask): the reference "
    r"(\d+)c rests on (\d+) external contracts \(< [\d.]+ x \d+\), the rest of the "
    r"book's is (\d+)c(?:; (no rung there before|our rung jumped (\d+)c in \d+m))? -- ")
_CLEAR = re.compile(r"^(\S+ \S+)Z \[IMM\] lone-ref guard (?:\(dry\) )?(\S+) (bid|ask): clear")
_BANNER = re.compile(r"^(\S+ \S+)Z === incentive_mm")
_MODE = re.compile(r"^(\S+ \S+)Z lone-reference guard \(2026-10-16\): (\w+)")


def low_priority() -> None:
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40)      # IDLE_PRIORITY_CLASS
    except Exception:
        pass


def _ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def day_window(day) -> tuple:
    """[00:00 ET, next 00:00 ET) of an ET date, as UTC epoch seconds."""
    a = imm.ET.localize(datetime(day.year, day.month, day.day))
    b = imm.ET.localize(datetime(day.year, day.month, day.day) + timedelta(days=1))
    return a.timestamp(), b.timestamp()


def utc_dates(a_ts: float, b_ts: float) -> list:
    d = datetime.fromtimestamp(a_ts, timezone.utc).date()
    last = datetime.fromtimestamp(b_ts - 1, timezone.utc).date()
    out = []
    while d <= last:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---- holds from the day logs -------------------------------------------------

def parse_episodes(lines, a_ts: float, b_ts: float) -> tuple:
    """(episodes, mode) from day-log lines in time order. A hold runs from its
    "side stood aside" line to the side's "clear" or the next process start
    (a restart forgets every hold), clipped to [a_ts, b_ts). `mode` is the
    last startup "lone-reference guard ...: <mode>" seen, None if none."""
    open_: dict = {}
    eps: list = []
    mode = None

    def close(key, t):
        e = open_.pop(key)
        if t > a_ts and e["start"] < b_ts:
            e["start"] = max(e["start"], a_ts)
            e["end"] = min(t, b_ts)
            eps.append(e)

    for line in lines:
        if "lone-re" not in line and "=== incentive_mm" not in line:
            continue
        m = _HOLD.match(line)
        if m:
            t = _ts(m.group(1))
            key = (m.group(2), m.group(3))
            if key not in open_ and t < b_ts:
                open_[key] = dict(ticker=key[0], side=key[1], start=t, ref=int(m.group(4)),
                                  top=int(m.group(5)), rest=int(m.group(6)),
                                  fresh=m.group(7) == "no rung there before",
                                  jump=int(m.group(8)) if m.group(8) else None)
            continue
        m = _CLEAR.match(line)
        if m:
            key = (m.group(2), m.group(3))
            if key in open_:
                close(key, _ts(m.group(1)))
            continue
        m = _BANNER.match(line)
        if m:
            t = _ts(m.group(1))
            for key in list(open_):
                close(key, t)
            continue
        m = _MODE.match(line)
        if m:
            mode = m.group(2)
    for key in list(open_):
        close(key, b_ts)
    return eps, mode


def day_log_lines(a_ts: float, b_ts: float):
    """Lines of every day log that can hold [a_ts - LOOKBACK, b_ts), oldest
    file first (a log is named by its process's local start date)."""
    files = sorted(glob.glob(os.path.join(imm.STATUS_DIR, "incentive-mm-*.log")))
    for fn in files:
        try:
            if os.path.getmtime(fn) < a_ts - LOOKBACK_SECS:
                continue
        except OSError:
            continue
        with open(fn, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line


# ---- reward given up, from the book snapshots ---------------------------------

def _levels(raw, dollars: bool) -> dict:
    by: dict = {}
    for p, q in raw or ():
        px = int(round(float(p) * 100)) if dollars else int(round(float(p)))
        by[px] = by.get(px, 0.0) + float(q)
    return by


def _own(raw) -> dict:
    by: dict = {}
    for p, q in raw or ():
        by[int(round(float(p)))] = by.get(int(round(float(p))), 0.0) + float(q)
    return by


def held_side_rate(full: dict, own: dict, sd: str, target: float, df: float, pool: float,
                   size: float) -> tuple:
    """($/day the held side gives up, touch - reference in cents) for one book
    snapshot: a rung of `size` put back at the external reference, scored
    with the bot's share model against the book as it is. None when the
    side has no reference."""
    other = "no" if sd == "yes" else "yes"
    ext = sorted(((px, q - own[sd].get(px, 0.0)) for px, q in full[sd].items()
                  if q - own[sd].get(px, 0.0) > 1e-6), key=lambda kv: -kv[0])
    ref = imm.side_reference_level(ext, target)
    if ref is None:
        return None
    lv = {s: sorted(full[s].items(), key=lambda kv: -kv[0]) for s in full}
    s_ot, q_ot = imm._side_share(lv[other], own[other], target, df)
    s_ac, q_ac = imm._side_share(lv[sd], own[sd], target, df)
    cf = dict(full[sd])
    cf[ref] = cf.get(ref, 0.0) + size
    s_cf, q_cf = imm._side_share(sorted(cf.items(), key=lambda kv: -kv[0]), {ref: size},
                                 target, df)
    frac_cf = (s_cf + s_ot) / 2.0 if (q_cf and q_ot) else 0.0
    frac_ac = (s_ac + s_ot) / 2.0 if (q_ac and q_ot) else 0.0
    return max(0.0, frac_cf - frac_ac) * pool, ext[0][0] - ref


def _ticker_of(line: str):
    i = line.find('"ticker":')
    if i < 0:
        return None
    j = line.find('"', i + 9)
    k = line.find('"', j + 1)
    return line[j + 1:k] if j >= 0 and k > j else None


def score_holds(eps: list, a_ts: float, b_ts: float, rows=None) -> None:
    """Add cost ($), class (at the touch / inside the book) and size to each
    episode from the managed book snapshots of [a_ts, b_ts). `rows` (tests)
    replaces the book_depth files with (ts, row-dict) pairs in time order."""
    by_key = defaultdict(list)
    for e in eps:
        by_key[(e["ticker"], "yes" if e["side"] == "bid" else "no")].append(e)
        e.setdefault("cost", 0.0)
        e.setdefault("dist", [])
    tickers = {k[0] for k in by_key}
    if rows is None:
        rows = _book_rows(tickers, a_ts, b_ts)
    last_size: dict = {}
    prev: dict = {}
    for ts, r in rows:
        t = r["ticker"]
        p = prev.get(t)
        if p:
            dt = max(0.0, min(ts - p[0], DT_CAP_SECS))
            for e, rate in p[1]:
                e["cost"] += rate * dt / 86400.0
        target = float(r.get("target") or 0.0)
        df = float(r.get("discount") or 0.5)
        pool = float(r.get("pool_per_day") or 0.0)
        dollars = r.get("unit", "dollars") == "dollars"
        full = {"yes": _levels(r.get("yes"), dollars), "no": _levels(r.get("no"), dollars)}
        own = {"yes": _own(r.get("own_yes_cents")), "no": _own(r.get("own_no_cents"))}
        rates = []
        for sd in ("yes", "no"):
            if own[sd]:
                last_size[(t, sd)] = sum(own[sd].values())
            held = [e for e in by_key.get((t, sd), ()) if e["start"] <= ts < e["end"]]
            if not held or target <= 0:
                continue
            e = held[0]
            other = "no" if sd == "yes" else "yes"
            size = last_size.get((t, sd)) or sum(own[other].values()) or FALLBACK_SIZE
            e.setdefault("size", size)
            got = held_side_rate(full, own, sd, target, df, pool, size)
            if got is None:
                continue
            rate, dist = got
            e["dist"].append(dist)
            rates.append((e, rate))
        prev[t] = (ts, rates)
    for e in eps:
        d = sorted(e.pop("dist"))
        e["cls"] = ("no snapshot" if not d else
                    "at the touch" if d[len(d) // 2] <= AT_TOUCH_C else "inside the book")
        e.setdefault("size", FALLBACK_SIZE)


def _book_rows(tickers: set, a_ts: float, b_ts: float):
    for d in utc_dates(a_ts, b_ts):
        files = sorted(glob.glob(os.path.join(imm.STATUS_DIR, "book_depth",
                                              f"book_depth_{d}_*.jsonl.gz")),
                       key=os.path.getmtime)
        for fn in files:
            try:
                fh = gzip.open(fn, "rt", encoding="utf-8")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"managed"' not in line or _ticker_of(line) not in tickers:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    ts = float(r.get("ts") or 0.0)
                    if r.get("source") == "managed" and a_ts <= ts < b_ts:
                        yield ts, r


# ---- Kalshi public reads ---------------------------------------------------------

_last_call = [0.0]


def _get(path: str, params: dict) -> dict:
    wait = TAPE_GAP_SECS - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    url = f"{KALSHI}{path}?{urllib.parse.urlencode(params)}" if params else f"{KALSHI}{path}"
    for i in range(6):
        _last_call[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "imm-lone-ref-monitor"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"429s on {path}")


def mid_now(ticker: str, cache: dict):
    if ticker not in cache:
        m = _get(f"/markets/{ticker}", {}).get("market") or {}
        b = float(m.get("yes_bid_dollars") or 0) * 100
        a = float(m.get("yes_ask_dollars") or 0) * 100
        cache[ticker] = (b + a) / 2 if b and a else (b or a or None)
    return cache[ticker]


def would_be_fills(eps: list, trades: list, mid) -> list:
    """(episode, contracts, $ vs mid) for each public trade that went through
    a held side's price while held: a taker sale at or under a held bid's
    reference, a taker buy at or over a held ask's (YES cents). Contracts are
    capped at the episode's rung size, spent as the trades come."""
    left = {id(e): e["size"] for e in eps}
    out = []
    for tr in sorted(trades, key=lambda x: x["created_time"]):
        tts = datetime.fromisoformat(tr["created_time"].replace("Z", "+00:00")).timestamp()
        yes = float(tr["yes_price_dollars"]) * 100
        for e in eps:
            if not (e["start"] <= tts < e["end"]) or left[id(e)] <= 0:
                continue
            if e["side"] == "bid" and tr.get("taker_side") == "no" and yes <= e["ref"]:
                pnl_c = (mid - e["ref"]) if mid is not None else 0.0
            elif e["side"] == "ask" and tr.get("taker_side") == "yes" and yes >= e["ref"]:
                pnl_c = (e["ref"] - mid) if mid is not None else 0.0
            else:
                continue
            n = min(float(tr.get("count_fp") or 0.0), left[id(e)])
            left[id(e)] -= n
            out.append((e, n, pnl_c * n / 100.0))
            break
    return out


def tape_check(eps: list, mids: dict) -> dict:
    """Would-be fills of the held sides, from one trades read per market."""
    by_t = defaultdict(list)
    for e in eps:
        by_t[e["ticker"]].append(e)
    res = dict(trades=0, contracts=0.0, usd=0.0, by_cls=defaultdict(float), top=[], done=True)
    t_stop = time.time() + TAPE_BUDGET_SECS
    for t, es in sorted(by_t.items()):
        if time.time() > t_stop:
            res["done"] = False
            break
        try:
            trades, cur = [], ""
            while True:
                params = {"ticker": t, "min_ts": int(min(e["start"] for e in es)),
                          "max_ts": int(max(e["end"] for e in es)), "limit": 1000}
                if cur:
                    params["cursor"] = cur
                r = _get("/markets/trades", params)
                trades += r.get("trades") or []
                cur = r.get("cursor") or ""
                if not cur:
                    break
            if not trades:
                continue
            got = would_be_fills(es, trades, mid_now(t, mids))
        except Exception as e:                      # one market never costs the email
            log(f"lone-ref monitor: tape read failed for {t}: {e!r}")
            continue
        for e, n, usd in got:
            res["trades"] += 1
            res["contracts"] += n
            res["usd"] += usd
            res["by_cls"][e["cls"]] += usd
            res["top"].append((usd, t, e["side"], e["ref"], n, e["cls"]))
    res["top"].sort()
    res["by_cls"] = dict(res["by_cls"])
    return res


# ---- fills the guard let through ------------------------------------------------

def jumped_fills(a_ts: float, b_ts: float, mids: dict, with_marks: bool = True) -> list:
    """Full-rung maker fills in [a_ts, b_ts) whose rung moved toward the touch
    by >= imm.LONE_REF_MIN_GAP_C within imm.LONE_REF_JUMP_WINDOW_MIN before
    the fill: the shape the guard is meant to stop. Each is
    (fill row, jump c, guarded?, $ vs mid now or None)."""
    win = imm.LONE_REF_JUMP_WINDOW_MIN * 60.0
    fills = []
    for d in utc_dates(a_ts, b_ts):
        path = os.path.join(imm.STATUS_DIR, f"fills_{d}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    f = json.loads(line)
                except ValueError:
                    continue
                if (a_ts <= f.get("ts", 0) < b_ts and not f.get("is_taker")
                        and not f.get("is_pad") and f.get("our_book_side")):
                    fills.append(f)
    if not fills:
        return []
    tickers = {f["ticker"] for f in fills}
    ev = defaultdict(list)
    for d in utc_dates(a_ts - win, b_ts):
        path = os.path.join(imm.STATUS_DIR, f"orders_{d}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if '"place"' not in line and '"amend"' not in line:
                    continue
                t = _ticker_of(line)
                if t not in tickers:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("kind") in ("place", "amend") and not r.get("is_pad"):
                    ts = datetime.fromisoformat(r["ts"]).timestamp()
                    ev[(t, r["book_side"])].append((ts, int(r["price_cents"])))
    out = []
    for f in fills:
        side = f["our_book_side"]
        px = f.get("our_price_cents") or round(f.get("yes_price_cents") or 0)
        w = [p for ts, p in ev.get((f["ticker"], side), ()) if f["ts"] - win <= ts <= f["ts"]]
        if not w:
            continue
        jump = (px - min(w)) if side == "bid" else (max(w) - px)
        left = f.get("our_left_before")
        full = left is not None and f["count"] >= float(left) - 0.01
        if jump < imm.LONE_REF_MIN_GAP_C or not full:
            continue
        usd = None
        if with_marks:
            try:
                m = mid_now(f["ticker"], mids)
                if m is not None:
                    usd = ((m - px) if side == "bid" else (px - m)) * f["count"] / 100.0
            except Exception as e:
                log(f"lone-ref monitor: mid read failed for {f['ticker']}: {e!r}")
        out.append((f, jump, imm.lone_ref_series(f.get("series") or f["ticker"].split("-")[0]),
                    usd))
    return out


# ---- the report ------------------------------------------------------------------

def load_store() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_store(data: dict) -> None:
    tmp = store_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, store_path())


def skip_fair() -> bool:
    """True while the bot leaves fair-gated markets out of the guard."""
    return bool(getattr(imm, "LONE_REF_SKIP_FAIR", False))


def fair_gate_of(series: str) -> str:
    """The fair gate the bot's quote loop runs on this series ('' = none),
    by the bot's own series checks: incentive_mm.fair_gate_of (2026-10-18,
    the list the guard skips), else the copy below of the gates in
    IncentiveMarketMaker.run_cycle as of 2026-10-17."""
    if hasattr(imm, "fair_gate_of"):
        return imm.fair_gate_of(series)
    t = series + "-X"

    def on(name: str, *args) -> bool:
        f = getattr(imm, name, None)
        try:
            return bool(f(*args)) if f is not None else False
        except Exception:
            return False

    checks = (
        ("carbon arc", getattr(imm, "CA_FAIR_ENABLE", False) and on("carbon_arc_settled", series)),
        ("openrouter tokens", getattr(imm, "OR_FAIR_ENABLE", False)
         and series in getattr(imm, "OR_FAIR_SERIES", ())),
        ("openrouter share", on("share_fair_series", series)),
        ("rain monthly", on("rain_monthly_series", series)),
        ("rain period", on("rain_period_gated", t)),
        ("snow monthly", on("snow_monthly_series", series)),
        ("cpi", on("cpi_fair_series", series)),
        ("rain weekend", on("rain_wknd_series", series)),
        ("rotten tomatoes", on("rt_fair_series", series)),
        ("gasbuddy", getattr(imm, "GB_FAIR_ENABLE", False) and on("gb_fair_series", series)),
        ("datacenter", getattr(imm, "DC_GATE_ENABLE", False) and on("datacenter_series", series)),
        ("quake", on("quake_gated", series)),
        ("vercel", on("vercel_series", series)),
        ("yt2", on("yt2_series", series)),
        ("yt weekly", on("ytw_series", series)),
        ("yt1", on("yt1_series", series)),
        ("treasury", on("treasury_gated_series", series)),
        ("mortgage", on("mort_series", series)),
        ("pokemon", on("poke_series", series)),
        ("nfl props", on("nfl_series", series)),
    )
    for name, hit in checks:
        if hit:
            return name
    return ""


def gate_totals(eps: list, top: list, gate_for) -> dict:
    """gate ('none' = no fair value) -> [holds, reward given up, blocked-fill $]
    from the day's holds and its would-be fills ((usd, ticker, ...) rows)."""
    out: dict = defaultdict(lambda: [0, 0.0, 0.0])
    for e in eps:
        g = out[gate_for(e["ticker"]) or "none"]
        g[0] += 1
        g[1] += e["cost"]
    for x in top:
        out[gate_for(x[1]) or "none"][2] += x[0]
    return {k: [v[0], round(v[1], 4), round(v[2], 4)] for k, v in out.items()}


def gate_net(v) -> float:
    return -v[2] - v[1]


def families() -> dict:
    """series -> family name, from the dashboard data object."""
    try:
        with open(dash_path(), encoding="utf-8") as f:
            html = f.read()
        i = html.index("const D = ") + len("const D = ")
        D, _ = json.JSONDecoder().raw_decode(html, i)
    except (OSError, ValueError) as e:
        log(f"lone-ref monitor: no dashboard families ({e!r})")
        return {}
    out: dict = {}
    for t, m in (D.get("markets") or {}).items():
        out.setdefault(t.split("-")[0], m.get("fam") or "")
    return out


def verdict(day: dict, run: dict) -> tuple:
    """(level, reasons): PROBLEM / WATCH / OK for one day and the running
    totals; the cost bars scale with the share of the day the guard covered."""
    reasons, level = [], 0

    def flag(lv, why):
        nonlocal level
        level = max(level, lv)
        reasons.append(why)

    frac = max(day["hours"] / 24.0, 1e-9)
    if day["mode"] != "on":
        flag(2, f"the guard reads {day['mode'] or 'unknown'} in the bot's startup line, not on")
    if day["let_thru_guarded"] <= LET_THRU_PROBLEM:
        flag(2, f"jumped fills on guarded series {usd(day['let_thru_guarded'])} to now")
    elif day["let_thru_guarded"] <= LET_THRU_WATCH:
        flag(1, f"jumped fills on guarded series {usd(day['let_thru_guarded'])} to now")
    if day["cost"] >= COST_PROBLEM_PER_DAY * frac:
        flag(2, f"reward given up {usd(day['cost'])} (bar {usd(COST_PROBLEM_PER_DAY * frac)})")
    elif day["cost"] >= COST_WATCH_PER_DAY * frac:
        flag(1, f"reward given up {usd(day['cost'])} (bar {usd(COST_WATCH_PER_DAY * frac)})")
    if day["net"] <= DAY_NET_WATCH:
        flag(1, f"the day's net {usd(day['net'])}: the holds cost more than they avoided")
    if day["max_flaps"] >= FLAP_WATCH:
        flag(1, f"{day['max_flaps_side']} held {day['max_flaps']} times (flapping)")
    if skip_fair():
        # fair-gated markets are out of the guard (2026-10-18): judge the
        # running net on the markets it still covers
        covered = gate_net((run.get("gates") or {}).get("none", [0, 0.0, 0.0]))
        if run.get("gate_days", 0.0) >= RUN_MIN_DAYS and covered <= RUN_NET_PROBLEM:
            flag(2, f"running net on the markets it guards (no fair gate) {usd(covered)} "
                    f"over {run['gate_days']:.1f} days")
    elif run["days"] >= RUN_MIN_DAYS and run["net"] <= RUN_NET_PROBLEM:
        flag(2, f"running net {usd(run['net'])} over {run['days']:.1f} days")
    if run["days"] >= RUN_MIN_DAYS and not skip_fair():
        for g, v in sorted((run.get("gates") or {}).items()):
            if g != "none" and gate_net(v) <= FAIR_GATE_WATCH:
                flag(1, f"fair-gated {g}: running net {usd(gate_net(v))} over "
                        f"{run['days']:.1f} days -- the guard costs more than it saves "
                        f"there; candidate to leave out")
    return LEVELS[level], reasons


def usd(x: float) -> str:
    return ("-$" if x < 0 else "+$") + f"{abs(x):,.0f}"


def build_report(day, with_tape: bool = True, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    a_ts, b_ts = day_window(day)
    a_ts = max(a_ts, GUARD_LIVE_TS)
    b_ts = min(b_ts, now.timestamp())
    eps, mode = parse_episodes(day_log_lines(a_ts, b_ts), a_ts, b_ts)
    if mode is None:
        _e, mode = parse_episodes(day_log_lines(a_ts - 7 * 86400, b_ts), a_ts, b_ts)
    for e in eps:
        e["dur"] = e["end"] - e["start"]
    score_holds(eps, a_ts, b_ts)
    mids: dict = {}
    tape = (tape_check(eps, mids) if with_tape and eps else
            dict(trades=0, contracts=0.0, usd=0.0, by_cls={}, top=[], done=True))
    jumped = jumped_fills(a_ts, b_ts, mids, with_marks=with_tape)
    fam = families()
    famof = lambda t: fam.get(t.split("-")[0]) or t.split("-")[0]
    flaps = defaultdict(int)
    for e in eps:
        flaps[(e["ticker"], e["side"])] += 1
    worst = max(flaps.items(), key=lambda kv: kv[1]) if flaps else (("", ""), 0)
    conc, t = [], a_ts
    while t < b_ts:
        conc.append(sum(1 for e in eps if e["start"] <= t < e["end"]))
        t += 60
    durs = sorted(e["dur"] for e in eps)
    cost = sum(e["cost"] for e in eps)
    g = [x for x in jumped if x[2]]
    s = [x for x in jumped if not x[2]]
    d = dict(day=day.isoformat(), a_ts=a_ts, b_ts=b_ts, hours=(b_ts - a_ts) / 3600.0,
             mode=mode, holds=len(eps), sides=len(flaps),
             markets=len({e["ticker"] for e in eps}),
             at_once=(sum(conc) / len(conc)) if conc else 0.0, at_once_max=max(conc or [0]),
             median_s=durs[len(durs) // 2] if durs else 0.0,
             max_flaps=worst[1], max_flaps_side=" ".join(worst[0]).strip(),
             cls={c: sum(1 for e in eps if e["cls"] == c) for c in
                  ("at the touch", "inside the book", "no snapshot")},
             cost=cost, would_be=tape["usd"], would_be_trades=tape["trades"],
             would_be_by_cls=dict(tape["by_cls"]),
             tape_done=tape["done"], tape_read=with_tape,
             let_thru_guarded=sum(x[3] or 0.0 for x in g), let_thru_guarded_n=len(g),
             let_thru_skipped=sum(x[3] or 0.0 for x in s), let_thru_skipped_n=len(s))
    d["net"] = -d["would_be"] - d["cost"]
    gate_cache: dict = {}

    def gate_for(tk: str) -> str:
        se = tk.split("-")[0]
        if se not in gate_cache:
            gate_cache[se] = fair_gate_of(se)
        return gate_cache[se]

    d["gates"] = gate_totals(eps, tape["top"], gate_for)
    by_fam = defaultdict(lambda: [0, 0.0, 0.0])
    for e in eps:
        b = by_fam[famof(e["ticker"])]
        b[0] += 1
        b[1] += e["dur"]
        b[2] += e["cost"]
    store = load_store()
    store[d["day"]] = d
    run_days = sum(v["hours"] for v in store.values()) / 24.0
    run = dict(days=run_days, net=sum(v["net"] for v in store.values()),
               cost=sum(v["cost"] for v in store.values()),
               avoided=sum(-v["would_be"] for v in store.values()),
               let_thru=sum(v["let_thru_guarded"] for v in store.values()))
    run_gates: dict = defaultdict(lambda: [0, 0.0, 0.0])
    # the days the per-gate split covers (it began 10/17)
    run["gate_days"] = sum(v["hours"] for v in store.values() if v.get("gates")) / 24.0
    for v in store.values():
        for g, x in (v.get("gates") or {}).items():
            r = run_gates[g]
            r[0] += x[0]
            r[1] += x[1]
            r[2] += x[2]
    run["gates"] = {k: list(v) for k, v in run_gates.items()}
    level, reasons = verdict(d, run)
    d["level"] = level
    return dict(d=d, run=run, level=level, reasons=reasons, store=store,
                top_flaps=sorted(flaps.items(), key=lambda kv: -kv[1])[:5],
                top_avoided=[x for x in tape["top"] if x[0] < 0][:5],
                top_blocked=sorted((x for x in tape["top"] if x[0] > 0), reverse=True)[:5],
                jumped=jumped, by_fam=dict(by_fam),
                by_cls=tape["by_cls"])


def subject_for(ctx) -> str:
    d = ctx["d"]
    day = datetime.strptime(d["day"], "%Y-%m-%d")
    return (f"IMM lone-ref guard {day:%m/%d}: {usd(d['net'])} "
            f"(blocking fills {usd(-d['would_be'])}, reward {usd(-d['cost'])}) | {ctx['level']}")


def fair_line(d: dict, run: dict) -> str:
    """Fair-gated vs no-fair-value markets, today and running, and the named
    families (Carbon Arc, Vercel, weekend rain) against the replay."""
    def split(gates):
        fair = [v for g, v in gates.items() if g != "none"]
        return (sum(gate_net(v) for v in fair),
                gate_net(gates.get("none", [0, 0.0, 0.0])))

    gt, rg = d.get("gates") or {}, run.get("gates") or {}
    f_d, n_d = split(gt)
    f_r, n_r = split(rg)
    named = "; ".join(
        f"{FAIR_NAMED_LABEL[g]} {usd(gate_net(gt.get(g, [0, 0.0, 0.0])))} "
        f"({usd(gate_net(rg.get(g, [0, 0.0, 0.0])))} running; replay "
        f"{usd(REPLAY_GATE_NET_PER_DAY[g])}/day)" for g in FAIR_NAMED)
    return (f"Fair value: markets with a fair gate net {usd(f_d)} today ({usd(f_r)} running), "
            f"markets without {usd(n_d)} ({usd(n_r)} running). {named}.")


def render_text(ctx) -> str:
    d, run = ctx["d"], ctx["run"]
    a = datetime.fromtimestamp(d["a_ts"], timezone.utc).astimezone(imm.ET)
    b = datetime.fromtimestamp(d["b_ts"], timezone.utc).astimezone(imm.ET)
    per_day = run["net"] / run["days"] if run["days"] > 0 else 0.0
    blocked = ("would have made" if d["would_be"] > 0 else "would have lost")
    by_cls = d.get("would_be_by_cls") or {}
    lines = [
        f"{ctx['level']}: net {usd(d['net'])}. The fills its holds blocked {blocked} "
        f"${abs(d['would_be']):,.0f}, and it gave up ${d['cost']:,.0f} of reward.",
        f"Running since 10/16 12:17 ET: {usd(run['net'])} over {run['days']:.1f} days "
        f"({usd(per_day)}/day; the replay said +${REPLAY_NET_PER_DAY:.0f}/day).",
    ]
    if skip_fair():
        covered = gate_net((run.get("gates") or {}).get("none", [0, 0.0, 0.0]))
        gd = run.get("gate_days", 0.0)
        lines.append(
            f"Fair-gated markets left the guard on 10/18. On the markets it still guards "
            f"(no fair gate): {usd(covered)} over {gd:.1f} days "
            f"({usd(covered / gd if gd > 0 else 0.0)}/day; the replay said "
            f"+${REPLAY_NO_FAIR_NET_PER_DAY:.0f}/day).")
    lines += [
        "",
        f"Window {a:%m/%d %H:%M}-{b:%H:%M} ET. Guard mode: {d['mode'] or 'unknown'}.",
        f"Held {d['holds']} times on {d['sides']} sides of {d['markets']} markets; "
        f"{d['at_once']:.0f} sides at once on average (max {d['at_once_max']}); "
        f"median hold {d['median_s']:.0f}s.",
        f"At the touch (the bait shape) {d['cls']['at the touch']}, inside the book "
        f"{d['cls']['inside the book']}; most-held side {d['max_flaps_side'] or '-'} "
        f"x{d['max_flaps']}.",
        f"Blocked fills: {d['would_be_trades']} public trades went through a held price; "
        f"those fills would be {usd(d['would_be'])} at the current mid"
        + (" (tape not read)" if not d.get("tape_read", True) else
           "" if d["tape_done"] else " (tape read cut short by its time budget)")
        + (" -- at the touch " + usd(by_cls.get("at the touch", 0.0)) + ", inside the book "
           + usd(by_cls.get("inside the book", 0.0)) if by_cls else "") + ".",
        fair_line(d, run),
        f"Let through: {d['let_thru_guarded_n']} jumped full-rung fills on guarded series "
        f"{usd(d['let_thru_guarded'])}; not guarded (mention books"
        + (", fair-gated markets" if skip_fair() else "")
        + f") {d['let_thru_skipped_n']} {usd(d['let_thru_skipped'])}.",
    ]
    if ctx["reasons"]:
        lines += ["", "NEEDS A LOOK:"] + [f"- {r}" for r in ctx["reasons"]]
    return "\n".join(lines)


def render_detail(ctx) -> str:
    out = ["by family: holds, minutes held, reward given up"]
    for f, b in sorted(ctx["by_fam"].items(), key=lambda kv: -kv[1][2]):
        out.append(f"  {f[:30]:30s} {b[0]:4d} holds {b[1] / 60:7.1f} min  ${b[2]:6.2f}")
    out.append("by fair gate: holds, reward given up, blocked-fill $, net today / running")
    rg = ctx["run"].get("gates") or {}
    for g, v in sorted((ctx["d"].get("gates") or {}).items(), key=lambda kv: gate_net(kv[1])):
        out.append(f"  {g[:20]:20s} {v[0]:5d} holds  ${v[1]:6.2f}  {usd(v[2]):>6s}  "
                   f"{usd(gate_net(v)):>6s} / {usd(gate_net(rg.get(g, v))):>6s}")
    out.append("most-held sides: " + ", ".join(f"{k[0]} {k[1]} x{v}" for k, v in ctx["top_flaps"]))
    out.append("losses its holds avoided: " + (", ".join(
        f"{t} {s} @{r} {n:.0f}ct {usd(u)} ({c})" for u, t, s, r, n, c in ctx["top_avoided"])
        or "none"))
    out.append("profits its holds blocked: " + (", ".join(
        f"{t} {s} @{r} {n:.0f}ct {usd(u)} ({c})" for u, t, s, r, n, c in ctx["top_blocked"])
        or "none"))
    for f, jump, guarded, u in ctx["jumped"]:
        ts = datetime.fromtimestamp(f["ts"], timezone.utc)
        out.append(f"  jumped fill {ts:%H:%M:%S}Z {f['ticker']} {f['our_book_side']} "
                   f"{f['count']:.0f} @ {f.get('our_price_cents')} (rung moved {jump}c) "
                   f"{'guarded' if guarded else 'not guarded'} "
                   + (usd(u) if u is not None else "unmarked"))
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker, no store")
    ap.add_argument("--day", help="ET day to report (YYYY-MM-DD); default yesterday")
    ap.add_argument("--no-tape", action="store_true",
                    help="skip the Kalshi public reads (no avoided fills, no marks)")
    args = ap.parse_args(argv)
    low_priority()

    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else today_et - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_lone_ref_monitor_sent_{today_et}.marker")
    if not (args.test or args.dry or args.day) and os.path.exists(marker):
        log(f"lone-ref email already sent for {today_et}; exiting")
        return 0
    ctx = build_report(day, with_tape=not args.no_tape, now=now_utc)
    text, subject = render_text(ctx), subject_for(ctx)
    log("lone-ref email: " + subject + "\n" + text + "\n\n-- detail --\n" + render_detail(ctx))
    if args.dry:
        return 0
    try:
        save_store(ctx["store"])
    except OSError as e:
        log(f"! lone_ref_monitor.json not written ({e!r})")
    attempts = 1 if args.test else 8
    alerter = imm.Alerter("IMM-LONEREF", live=True)
    if not alerter.enabled:
        log("cannot send lone-ref email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text + "\n\n-- detail --\n" + render_detail(ctx),
                                  subject=subject)
        if ok:
            break
        log(f"lone-ref send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"lone-ref send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.day):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR, "imm_lone_ref_monitor_sent_*.marker")):
            name = os.path.basename(old)[len("imm_lone_ref_monitor_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
