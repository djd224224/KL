#!/usr/bin/env python3
r"""send_bol_report.py -- the daily back-of-line email (Jack 2026-10-22: "Give
me Daily email report on it", after "What's impact of the back-of-the-line
change?").

BACK OF THE LINE (incentive_mm BACK_OF_LINE, on since 20:46:54Z 10/17): the
bot re-queues its resting orders behind the other makers at the same price
(+1 then -1 contract, which forfeits queue priority), so a taker that stops
short of the level fills the queue ahead of us first. It costs writes, not
rewards (Kalshi's scoring ignores queue position). The 10/17 design replay
put it at ~$23/day; the 10/21 live replay (the method below) at +$22.65.

ONE EMAIL per ET day, ending in OK / WATCH / PROBLEM:
  saved     each move's window -- until the same order's next cancel / gone,
            price change or size-up amend (either forfeits the spot in both
            worlds), its next move, or WINDOW_SECS -- replays the public tape
            at its price and side from the OLD queue spot (the contracts that
            were ahead; never shortened by cancels, so a floor) against the
            order's REAL maker fills in the window; avoided = old-spot fills -
            real fills, each valued at the market's last trade MARK_SECS
            later. saved = losing fills avoided - winning fills given up.
  cost      back-of-line amends against every order write the bot logged
  families  the day by dashboard family (Trump mentions are ~half the moves)
  running   the last 7 days from bol_report.json

SOURCES, read-only: back_of_line_ / orders_ / fills_<UTC date>.jsonl, the
dashboard data object (family names) and Kalshi's PUBLIC GET /markets/trades
(no auth, paced, TAPE_BUDGET_SECS). The files written: the sent-marker,
bol_report.json and the tape cache bol_report_cache/<day>/ (kept 3 days).
Imports imm_quote_gaps first, like the other morning emails, so the launcher
env and the alert credentials apply. Runs at idle priority: the bot shares
the PC. Only BOL order ids are kept from the orders sink (~640k rows a day).

USAGE:
  python send_bol_report.py                   # the scheduled daily run
  python send_bol_report.py --dry             # build + print, send nothing
  python send_bol_report.py --test            # send now, ignore the marker
  python send_bol_report.py --day 2026-10-21  # a specific ET day
Scheduled daily as "KL imm back-of-line" (register_imm_back_of_line.ps1),
06:45 ET: the tape takes ~25-35 min and the marks need 04:00 ET.
All console output is ASCII (the task console is cp1252).
"""
import argparse
import bisect
import ctypes
import glob
import json
import os
import shutil
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

KALSHI = os.environ.get("IMM_BOL_REPORT_KALSHI",
                        "https://api.elections.kalshi.com/trade-api/v2")
WINDOW_SECS = 300.0
MARK_SECS = 4 * 3600.0
TAPE_GAP_SECS = 0.2
TAPE_BUDGET_SECS = 2700.0
CACHE_KEEP_DAYS = 3
RUN_DAYS = 7
PROBLEM_PER_DAY = -10.0         # 7-day saved per day at or below this: PROBLEM
PROBLEM_MIN_DAYS = 3
COVERAGE_WATCH = 0.9            # tape read for under this share of the moved markets
LEVELS = ("OK", "WATCH", "PROBLEM")


def store_path() -> str:
    """Resolved at call time, so a sandboxed STATUS_DIR (tests) is honoured."""
    return os.path.join(imm.STATUS_DIR, "bol_report.json")


def cache_dir(day) -> str:
    return os.path.join(imm.STATUS_DIR, "bol_report_cache", day.isoformat())


def dash_path() -> str:
    return os.path.join(imm.STATUS_DIR, "dashboard", "imm_dashboard.html")


def low_priority() -> None:
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40)      # IDLE_PRIORITY_CLASS
    except Exception:
        pass


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


def _iso(s) -> float:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


def _jsonl(name: str, dates: list):
    for d in dates:
        path = os.path.join(imm.STATUS_DIR, f"{name}_{d}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    yield json.loads(ln)
                except ValueError:
                    continue


# ---- the day's moves, the orders' ends and fills, the write counts -----------

def load_moves(a_ts: float, b_ts: float) -> list:
    """The live moves of [a_ts, b_ts) that the exchange took (ok)."""
    return [r for r in _jsonl("back_of_line", utc_dates(a_ts, b_ts))
            if r.get("mode") == "on" and r.get("ok") and a_ts <= float(r.get("ts") or 0) < b_ts]


def load_orders(oids: set, a_ts: float, b_ts: float) -> tuple:
    """({order id: sorted end times}, write counts of [a_ts, b_ts)). An order's
    spot ends at a cancel / gone, or an amend that changes its price or raises
    its size (a size cut keeps the spot); the reads run WINDOW_SECS past b_ts."""
    ends = defaultdict(list)
    writes = defaultdict(int)
    for r in _jsonl("orders", utc_dates(a_ts, b_ts + WINDOW_SECS)):
        k = r.get("kind")
        try:
            t = _iso(r["ts"])
        except (KeyError, TypeError, ValueError):
            continue
        if a_ts <= t < b_ts and k in ("place", "amend", "cancel", "gone", "reject"):
            writes["all"] += 1
            if k == "amend" and r.get("reason") in ("back_of_line", "back_of_line_restore"):
                writes["bol"] += 1
        oid = r.get("order_id")
        if oid not in oids:
            continue
        if k in ("cancel", "gone"):
            ends[oid].append(t)
        elif k == "amend" and not r.get("reason"):
            pp, pc = r.get("prev_price_cents"), r.get("prev_count")
            try:
                up = pc is not None and float(r.get("count") or 0) > float(pc) + 1e-9
            except (TypeError, ValueError):
                up = False
            if (pp is not None and pp != r.get("price_cents")) or up:
                ends[oid].append(t)
    for v in ends.values():
        v.sort()
    return dict(ends), dict(writes)


def load_fills(oids: set, a_ts: float, b_ts: float) -> dict:
    """{order id: [(ts, contracts)]} of our maker fills."""
    out = defaultdict(list)
    for r in _jsonl("fills", utc_dates(a_ts, b_ts + WINDOW_SECS)):
        if r.get("is_taker") or r.get("order_id") not in oids:
            continue
        try:
            out[r["order_id"]].append((float(r["ts"]), float(r.get("count") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    return dict(out)


def families() -> dict:
    """series -> family name, from the dashboard data object."""
    try:
        with open(dash_path(), encoding="utf-8") as f:
            html = f.read()
        i = html.index("const D = ") + len("const D = ")
        D, _ = json.JSONDecoder().raw_decode(html, i)
    except (OSError, ValueError) as e:
        log(f"bol report: no dashboard families ({e!r})")
        return {}
    out: dict = {}
    for t, m in (D.get("markets") or {}).items():
        out.setdefault(t.split("-")[0], m.get("fam") or "")
    return out


# ---- the public tape ------------------------------------------------------------

_last_read = [0.0]


def _get(path: str, params: dict) -> dict:
    for attempt in range(8):
        wait = TAPE_GAP_SECS - (time.time() - _last_read[0])
        if wait > 0:
            time.sleep(wait)
        _last_read[0] = time.time()
        url = f"{KALSHI}{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": "KL-IMM-bol-report/1.0"}), timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 + 2 * attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2 + attempt)
    raise RuntimeError(f"public read kept failing: {path}")


def _trade(t: dict) -> list:
    c = t.get("count_fp", t.get("count"))
    px = t.get("yes_price")
    if px is None and t.get("yes_price_dollars") is not None:
        px = round(float(t["yes_price_dollars"]) * 100, 2)
    return [_iso(t["created_time"]), float(c or 0), float(px), t.get("taker_side")]


def fetch_tape(ticker: str, a_ts: float, b_ts: float, cache: str) -> list:
    """Every public trade of `ticker` in [a_ts, b_ts], oldest first, as
    [ts, contracts, YES cents, taker side]; cached per day."""
    path = os.path.join(cache, ticker.replace("/", "_") + ".json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    out, cursor = [], None
    while True:
        p = {"ticker": ticker, "min_ts": int(a_ts), "max_ts": int(b_ts) + 1, "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        js = _get("/markets/trades", p)
        out += [_trade(t) for t in js.get("trades") or []]
        cursor = js.get("cursor")
        if not cursor or not js.get("trades"):
            break
    out.sort()
    try:
        os.makedirs(cache, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f)
    except OSError:
        pass
    return out


# ---- the replay -------------------------------------------------------------------

def replay_move(move: dict, tape: list, times: list, end: float, real: float) -> dict:
    """One move against the tape (oldest first, `times` its timestamps) up to
    `end`: the OLD spot's fills (queue ahead `ahead`, never shortened by
    cancels), the order's REAL fills, avoided = max(0, old - real) valued at
    the old spot's average take value (the market's last trade MARK_SECS
    after each take)."""
    side, px = move["side"], float(move["px"])
    hit_by = "no" if side == "bid" else "yes"      # YES bid <- NO takers; ask <- YES takers
    qa, rem = float(move["ahead"]), float(move["rem"])
    old, val, traded = 0.0, 0.0, False
    for k in range(bisect.bisect_left(times, float(move["ts"])), len(tape)):
        ts, c, tpx, tside = tape[k]
        if ts >= end:
            break
        if tside != hit_by or abs(tpx - px) > 1e-6:
            continue
        traded = True
        take = min(rem, max(0.0, c - qa))
        qa = max(0.0, qa - c)
        if take <= 0:
            continue
        rem -= take
        j = bisect.bisect_left(times, ts + MARK_SECS) - 1
        mark = tape[max(0, min(len(tape) - 1, j))][2]
        val += take * ((mark - px) if side == "bid" else (px - mark)) / 100.0
        old += take
    avoided = max(0.0, old - real)
    per = val / old if old > 0 else 0.0
    return {"traded": traded or real > 0, "old": old, "real": real,
            "avoided": avoided, "value": avoided * per}


def build_report(day, with_tape: bool = True, now=None) -> dict:
    """The day's numbers, the running totals and the store to save."""
    a_ts, b_ts = day_window(day)
    moves = load_moves(a_ts, b_ts)
    oids = {m.get("order_id") for m in moves}
    ends, writes = load_orders(oids, a_ts, b_ts)
    fills = load_fills(oids, a_ts, b_ts)
    fam_of = families()
    by_t = defaultdict(list)
    for m in moves:
        by_t[m["ticker"]].append(m)
    restore_fails = sum(1 for m in moves if not m.get("restored"))
    t0 = time.time()
    covered = 0
    fam = defaultdict(lambda: defaultdict(float))
    tot = defaultdict(float)
    cache = cache_dir(day)
    for tk in sorted(by_t, key=lambda t: -len(by_t[t])):
        mv = sorted(by_t[tk], key=lambda r: float(r["ts"]))
        f = fam_of.get(tk.split("-")[0]) or "other"
        fam[f]["moves"] += len(mv)
        tot["moves"] += len(mv)
        if not with_tape or time.time() - t0 > TAPE_BUDGET_SECS:
            continue
        try:
            tape = fetch_tape(tk, float(mv[0]["ts"]), float(mv[-1]["ts"]) + WINDOW_SECS + MARK_SECS,
                              cache)
        except Exception as e:
            log(f"bol report: ! tape {tk}: {e}")
            continue
        covered += 1
        times = [x[0] for x in tape]
        nxt, seen = {}, {}
        for r in reversed(mv):
            nxt[id(r)] = seen.get(r["order_id"])
            seen[r["order_id"]] = float(r["ts"])
        for r in mv:
            ts0 = float(r["ts"])
            end = ts0 + WINDOW_SECS
            if nxt[id(r)] is not None:
                end = min(end, nxt[id(r)])
            e = ends.get(r["order_id"]) or []
            j = bisect.bisect_left(e, ts0)
            if j < len(e):
                end = min(end, e[j])
            real = sum(c for fts, c in fills.get(r["order_id"], ()) if ts0 <= fts < end + 1)
            x = replay_move(r, tape, times, end, real)
            for k, v in (("traded", float(x["traded"])), ("mattered", float(x["avoided"] > 0)),
                         ("avoided_ct", x["avoided"]), ("value", x["value"]),
                         ("losses_avoided", -min(0.0, x["value"])),
                         ("wins_forgone", max(0.0, x["value"]))):
                tot[k] += v
                fam[f][k] += v
    day_rec = {"day": day.isoformat(), "moves": int(tot["moves"]), "markets": len(by_t),
               "covered": covered, "traded": int(tot["traded"]), "mattered": int(tot["mattered"]),
               "avoided_ct": round(tot["avoided_ct"], 1),
               "saved": round(-tot["value"], 2),
               "losses_avoided": round(tot["losses_avoided"], 2),
               "wins_forgone": round(tot["wins_forgone"], 2),
               "bol_amends": int(writes.get("bol", 0)), "writes": int(writes.get("all", 0)),
               "restore_fails": restore_fails, "with_tape": bool(with_tape),
               "by_family": {k: {"moves": int(v["moves"]), "saved": round(-v["value"], 2),
                                 "mattered": int(v["mattered"])} for k, v in fam.items()}}
    store = load_store()
    if with_tape:
        store.setdefault("days", {})[day.isoformat()] = day_rec
    run_days = sorted(d for d in (store.get("days") or {})
                      if (day - timedelta(days=RUN_DAYS - 1)).isoformat() <= d <= day.isoformat())
    run = {"days": len(run_days),
           "saved": round(sum(store["days"][d]["saved"] for d in run_days), 2) if run_days else 0.0}
    run["per_day"] = round(run["saved"] / run["days"], 2) if run["days"] else 0.0
    return {"day": day, "d": day_rec, "run": run, "store": store,
            "now": now or datetime.now(timezone.utc)}


# ---- the store, the verdict, the email ------------------------------------------

def load_store() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_store(data: dict) -> None:
    tmp = store_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, store_path())


def prune_cache(today) -> None:
    root = os.path.join(imm.STATUS_DIR, "bol_report_cache")
    for path in glob.glob(os.path.join(root, "*")):
        try:
            d = datetime.strptime(os.path.basename(path), "%Y-%m-%d").date()
        except ValueError:
            continue
        if d < today - timedelta(days=CACHE_KEEP_DAYS):
            shutil.rmtree(path, ignore_errors=True)


def verdict(d: dict, run: dict) -> tuple:
    if (run["days"] >= PROBLEM_MIN_DAYS and run["per_day"] <= PROBLEM_PER_DAY):
        return "PROBLEM", (f"back-of-line has given up more winning fills than losing ones: "
                           f"{usd(run['per_day'])}/day over {run['days']} days. "
                           f"IMM_BACK_OF_LINE=dry takes it out (env => restart_imm.ps1 -Task).")
    why = []
    if d["with_tape"] and d["saved"] < 0:
        why.append(f"the day gave up {usd(-d['saved'])} more in winning fills than it saved")
    if d["restore_fails"]:
        why.append(f"{d['restore_fails']} move(s) were not restored (+1 contract left resting)")
    if d["with_tape"] and d["markets"] and d["covered"] < COVERAGE_WATCH * d["markets"]:
        why.append(f"the tape covered {d['covered']} of {d['markets']} moved markets")
    if why:
        return "WATCH", "; ".join(why) + "."
    return "OK", (f"{usd(run['per_day'])}/day saved over {run['days']} day(s); "
                  f"nothing to change.")


def usd(x: float) -> str:
    x = round(float(x), 2) + 0.0          # no "$-0.00" for a family that changed nothing
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def write_share(d: dict) -> str:
    return f"{100.0 * d['bol_amends'] / d['writes']:.0f}%" if d["writes"] else "n/a"


def subject_for(ctx) -> str:
    d, (level, _why) = ctx["d"], verdict(ctx["d"], ctx["run"])
    head = (f"saved {usd(d['saved'])} at 4h" if d["with_tape"] else "no tape")
    return (f"IMM back-of-line {ctx['day'].strftime('%m/%d')}: {head} | "
            f"{d['moves']:,} moves, {write_share(d)} of writes | {level}")


def render_text(ctx) -> str:
    d, run, day = ctx["d"], ctx["run"], ctx["day"]
    level, why = verdict(d, run)
    out = [f"IMM back-of-line -- {day.strftime('%a %Y-%m-%d')} (ET)", ""]
    if d["with_tape"]:
        out.append(f"SAVED at 4h: {usd(d['saved'])} net -- {usd(d['losses_avoided'])} of losing "
                   f"fills avoided, {usd(d['wins_forgone'])} of winning fills given up.")
        out.append(f"  {d['mattered']:,} of {d['moves']:,} moves changed a fill "
                   f"({d['traded']:,} saw a trade at our price before the order changed; "
                   f"{d['avoided_ct']:,.0f} contracts).")
    else:
        out.append("No tape read (--no-tape): the saved figure is not computed.")
    out.append(f"COST: {d['bol_amends']:,} back-of-line amends = {write_share(d)} of the bot's "
               f"{d['writes']:,} order writes; {d['restore_fails']} restore fail(s). "
               f"Rewards are unaffected (Kalshi's scoring ignores queue position).")
    out.append(f"{run['days']} DAY(S): {usd(run['saved'])} saved ({usd(run['per_day'])}/day).")
    out.append("")
    out.append("By family (saved at 4h | moves | moves that changed a fill):")
    fams = sorted(d["by_family"].items(), key=lambda kv: (-kv[1]["saved"], -kv[1]["moves"]))
    for name, v in fams:
        share = 100.0 * v["moves"] / d["moves"] if d["moves"] else 0.0
        out.append(f"  {name[:26]:26s} {usd(v['saved']):>10s} {v['moves']:8,} ({share:4.1f}%) "
                   f"{v['mattered']:5,}")
    out.append("")
    out.append(f"Verdict: {level} -- {why}")
    out.append("")
    out.append("How it is measured: each move's window runs until the order's next cancel, price "
               "change or size-up, its next move, or 5 minutes. The public tape at its price is "
               "replayed from the old queue spot (the contracts that were ahead, never shortened "
               "by cancels -- a floor) against the order's real fills; the difference is valued "
               "at the market's last trade 4 hours later. "
               f"Tape read for {d['covered']} of {d['markets']} moved markets.")
    out.append("Off switch: IMM_BACK_OF_LINE=dry (env => restart_imm.ps1 -Task).")
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
                    help="skip the Kalshi public reads (no saved figure)")
    args = ap.parse_args(argv)
    low_priority()

    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else today_et - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_bol_report_sent_{today_et}.marker")
    if not (args.test or args.dry or args.day) and os.path.exists(marker):
        log(f"back-of-line email already sent for {today_et}; exiting")
        return 0
    ctx = build_report(day, with_tape=not args.no_tape, now=now_utc)
    text, subject = render_text(ctx), subject_for(ctx)
    log("back-of-line email: " + subject + "\n" + text)
    if args.dry:
        return 0
    if not args.no_tape:
        try:
            save_store(ctx["store"])
        except OSError as e:
            log(f"! bol_report.json not written ({e!r})")
    prune_cache(today_et)
    attempts = 1 if args.test else 8
    alerter = imm.Alerter("IMM-BOL", live=True)
    if not alerter.enabled:
        log("cannot send back-of-line email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject)
        if ok:
            break
        log(f"back-of-line send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"back-of-line send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.day):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR, "imm_bol_report_sent_*.marker")):
            name = os.path.basename(old)[len("imm_bol_report_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
