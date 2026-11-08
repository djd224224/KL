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
at its program's end; a later program needs a re-run -- the hourly Windows
task "KL cheap-scan" (run_cheap_scan_hidden.vbs; Jack 2026-11-08: "ship and
list at $50, give daily email report on performance. add a schedule") does
that. A scheduled re-run never drops a market it could not read (a failed
/markets batch or book keeps the entry), and a market already listed for its
current program period keeps its entry to the period's end while its rung
still pays, whatever it projects for the hours left (the $1 counts the whole
period). Log: run-logs/cheap-scan/scan.log.

NEW SERIES FOR JACK TO APPROVE (Jack 2026-11-08: "Should the daily email flag
new far-dated program series for you to approve?" -> "Add it"):

    python cheap_scan.py --discover               # before the 08:30 email
    python cheap_scan.py --approve KXFOO [--write]  # quote it (--write: list now)
    python cheap_scan.py --reject KXFOO --why "answer lands in March"

--discover scores every liquidity-program market OUTSIDE the list's series
(the --series prefixes plus the approved series) the same way -- 6M+ by
Kalshi's dates, a 1-2c rung that projects --bar at --max-usd a market, no
orders of yours there, the bot not earning more -- and writes the series with
at least one such market to run-logs/cheap-scan/discover.json; the daily email
(send_cheap_report.py) shows them, NEW the first day a series pays. It never
lists anything: whether the answer is really 6M+ away is Jack's call.
--approve puts a series in scope for the hourly scan (exact series ticker);
--reject keeps it out of the email (and off the list, if it was on it). The
decisions live in run-logs/cheap-scan/series.json; REJECTED_1108 holds the
11/08 review's far-dated payers that are not truly far.
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
OUT_DIR = os.path.join(HERE, "run-logs", "cheap-scan")
LOCK = os.path.join(OUT_DIR, "scan.lock")
SERIES_FILE = os.path.join(OUT_DIR, "series.json")
DISCOVER_OUT = os.path.join(OUT_DIR, "discover.json")
DISCOVER_LOCK = os.path.join(OUT_DIR, "discover.lock")
# the truly-far set (run_cheap_scan_hidden.vbs passes the same list)
TRULY_FAR = ("KXDSENATESEATS,KXSTATE51,KXSCOTUSPOWER,KXFRENCHPRES,POPVOTEMOV,KXSENATESEATSD,"
             "ECMOV,KXMOONMAN,KXPRIMEENGCONSUMPTION")
NEW_HOURS = 26.0                 # a series first seen paying this recently is NEW in the email
MAX_DISCOVER_BOOKS = 1500        # the most books one --discover reads (the biggest pools first)
NEAR_BEST = 0.10                 # --discover reads a book only when a side's best is within 10c of 0
# The 11/08 review's far-dated series that pay a 1-2c rung but are NOT truly
# far (Jack 2026-11-08: "Truly far: Dem Senate seats 2029, 51st state, Supreme
# Court makeup 2029, French presidency 2027 ^these all fit. the others dont"):
# Kalshi dates them 6M+ out, the answer lands sooner. --discover leaves them
# out of the email; a trailing * covers the family; --approve overrides one.
REJECTED_1108 = {
    "KXGRAMMYNOM*": "Grammy nominations, announced 11/16 (Kalshi: 358 days out)",
    "KXBOND": "next James Bond: casting news can land any day",
    "KXLAMAYORMOV": "LA mayor margin: decided by the 11/03 vote",
    "KXNEXTSPEAKER": "next Speaker: the vote is Jan 3",
    "KXANTHROPICLEADLEFT": "Anthropic IPO underwriter: news can land any day",
    "KXJOINCLUB": "Haaland's next club: transfer news every window",
    "KXMUSICREPORT": "Luminate year-end report: the year's data is mostly in",
    "KXBOZPERMITS": "Bozeman permits: monthly data",
    "KXUCLLEADER": "Champions League leaders: weekly information (borderline)",
    "KXMILEAD": "left out 11/08",
    # the first --discover flag, 11/08 21:05Z (Jack: "KXPNP is not truly far out because it
    # can be settled tomororw"): truly far = the answer cannot land before 6M out
    "KXPNP": "P vs NP before 2028: a proof would settle it the day it lands",
}


def log(msg: str) -> None:
    line = f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "scan.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def ts(s) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def zs(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def series_of(t: str) -> str:
    return str(t).split("-")[0]


def pat_match(series: str, pats):
    """The first pattern in `pats` naming `series`: an exact series ticker, or a
    family ending in * (KXGRAMMYNOM* covers KXGRAMMYNOMAOTY)."""
    for p in pats:
        if (p.endswith("*") and series.startswith(p[:-1])) or series == p:
            return p
    return None


def load_series_state(strict: bool = False):
    """(state, ok): run-logs/cheap-scan/series.json -- {"approved": {series:
    {at, note}}, "rejected": {series: {at, why}}, "seen": {series: {first_seen,
    last_seen, ...}}}. A missing file is an empty state (ok). An unreadable one
    raises when strict, else reads as empty with ok False (nothing may be
    saved over it)."""
    ok = True
    try:
        with open(SERIES_FILE, encoding="utf-8") as fh:
            st = json.load(fh) or {}
        if not isinstance(st, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        st = {}
    except (OSError, ValueError) as e:
        if strict:
            raise
        log(f"! {SERIES_FILE} unreadable ({e!r}): no approved series this run, nothing saved over it")
        st, ok = {}, False
    for k in ("approved", "rejected", "seen"):
        if not isinstance(st.get(k), dict):
            st[k] = {}
    return st, ok


def save_series_state(st) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    tmp = SERIES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=1, sort_keys=True)
    os.replace(tmp, SERIES_FILE)


def scope(series_arg: str, st):
    """(prefixes, approved, keep): the hourly scan's markets -- a series an
    approval names exactly, else one a --series prefix starts, unless Jack
    rejected that series."""
    prefixes = tuple(s.strip() for s in (series_arg or "").split(",") if s.strip())
    approved = set(st.get("approved") or {})
    rejected = tuple(st.get("rejected") or {})

    def keep(t: str) -> bool:
        s = series_of(t)
        if s in approved:
            return True
        return s.startswith(prefixes) and not pat_match(s, rejected)
    return prefixes, approved, keep


def rd(path, params=None):
    for i in range(3):
        try:
            return kalshi_reads.read(path, params or {})
        except Exception as ex:  # noqa: BLE001
            err = ex
            time.sleep(2 + 2 * i)
    print(f"! read failed {path}: {err}", file=sys.stderr)
    return None


def programs(client, keep, now):
    """{market: {pd, start, end, target, df}} for the active liquidity programs
    on the markets `keep(ticker)` accepts."""
    per = {}
    for p in snp.fetch_programs_raw(client):
        t = p.get("market_ticker") or ""
        if p.get("incentive_type") != "liquidity" or not p.get("end_date") or not p.get("start_date"):
            continue
        if ts(p["end_date"]) <= now or not keep(t):
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
    """The bot's resting orders per market (book cents, exact cents), and the
    markets where the account rests orders the bot did not place (Jack's own:
    the bot stands off a market with one -- its manual standoff -- so a rung
    listed there would never rest)."""
    own, exact, foreign, cursor = {}, {}, set(), None
    for _ in range(60):
        q = {"status": "resting", "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        r = client.get("/portfolio/orders", params=q) or {}
        for o in r.get("orders") or []:
            if not str(o.get("client_order_id", "")).startswith(imm.CLIENT_ORDER_PREFIX + "-"):
                if o.get("ticker"):
                    foreign.add(o["ticker"])
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
    return own, exact, foreign


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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default=TRULY_FAR,
                    help="comma-separated series PREFIXES (the truly-far set; default the scheduled list)")
    ap.add_argument("--min-days", type=float, default=182.0, help="horizon backstop (6M)")
    ap.add_argument("--max-usd", type=float, default=50.0, help="size x px a market, at most")
    ap.add_argument("--bar", type=float, default=1.10, help="$ the rung must project by its program end")
    ap.add_argument("--include-quoted", action="store_true",
                    help="list a market even where the bot earns more under its family rules")
    ap.add_argument("--report", help="write every scored market here (.json)")
    ap.add_argument("--write", action="store_true", help="write the list (the bot hot-loads it)")
    ap.add_argument("--discover", action="store_true",
                    help="score the program series OUTSIDE the list for the daily email (writes discover.json)")
    ap.add_argument("--approve", help="comma-separated series tickers to put in scope (with --write: list now)")
    ap.add_argument("--reject", help="comma-separated series tickers to keep out of the email and off the list")
    ap.add_argument("--why", default="", help="the reason, kept with an --approve / --reject")
    a = ap.parse_args(argv)
    os.makedirs(OUT_DIR, exist_ok=True)
    if a.approve or a.reject:
        lock, fn = LOCK, decide
    elif a.discover:
        lock, fn = DISCOVER_LOCK, discover
    else:
        lock, fn = LOCK, scan
    if os.path.exists(lock) and time.time() - os.path.getmtime(lock) < 1800:
        log(f"another cheap scan is running ({os.path.basename(lock)}) -- skipped")
        return 1 if fn is decide else 0
    open(lock, "w").close()
    try:
        return fn(a)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def scan(a) -> int:
    st, _ok = load_series_state()
    prefixes, approved, keep = scope(a.series, st)
    names = ",".join(prefixes) + (f" + approved {','.join(sorted(approved))}" if approved else "")
    now = datetime.now(timezone.utc)
    try:
        with open(imm.cheap_path(), encoding="utf-8") as fh:
            listed_now = (json.load(fh) or {}).get("markets") or {}
    except (OSError, ValueError):
        listed_now = {}
    client = imm._fair_reader_client()
    progs = programs(client, keep, now)
    tick = sorted(progs)
    mk = {}
    for i in range(0, len(tick), 100):
        j = rd("/markets", {"tickers": ",".join(tick[i:i + 100]), "limit": 1000}) or {}
        for m in j.get("markets") or []:
            mk[m["ticker"]] = m
    far, unread = {}, set()
    for t in tick:
        m = mk.get(t) or {}
        if not m:
            unread.add(t)               # its /markets batch failed: a listed entry stays
            continue
        if m.get("status") not in ("active", "open") or imm.IncentiveMarketMaker._blocked(t):
            continue
        ds = [ts(m[k]) for k in ("expected_expiration_time", "close_time") if m.get(k)]
        if ds and (min(ds) - now).total_seconds() / 86400 >= a.min_days:
            far[t] = (m, min(ds))
    own, own_exact, foreign = own_orders(client)

    def book(t):
        ob = rd(f"/markets/{t}/orderbook")
        time.sleep(0.2)
        return t, ob
    with ThreadPoolExecutor(4) as ex:
        books = dict(ex.map(book, sorted(far)))

    rows, why = [], {"no book": 0, "no paying 1-2c rung": 0, "under bar": 0, "bot earns more": 0,
                     "your orders there": 0}
    for t, (m, hz) in sorted(far.items()):
        ob = books.get(t)
        if not ob:
            why["no book"] += 1
            unread.add(t)               # a failed book read: a listed entry stays
            continue
        pr = progs[t]
        hrs = (pr["end"] - max(now, pr["start"])).total_seconds() / 3600
        # our own cheap rung, netted out of the book like the rest of our orders
        pick, cur = best_rung(t, pr, ob, own.get(t, []), own_exact.get(t, []), a.max_usd, hrs)
        # listed for this same program period: kept to its end while it pays
        same = (listed_now.get(t) or {}).get("stop") == zs(pr["end"])
        if t in listed_now:
            cur = 0.0                   # what the bot earns there now is this list's rung
        r = {"ticker": t, "event": m.get("event_ticker"), "sub": (m.get("yes_sub_title") or "")[:40],
             "horizon": zs(hz), "stop": zs(pr["end"]), "period_start": zs(pr["start"]),
             "hrs_left": round(hrs, 1), "same_period": same,
             "pool_day": round(pr["pd"], 2), "bot_usd_day_now": round(cur or 0.0, 2),
             "bot_resting": sum(q for _s, _p, q in own.get(t, [])), "pick": pick}
        if pick is None:
            why["no paying 1-2c rung"] += 1
            r["verdict"] = "none"
        elif t in foreign:
            # (the bot's manual standoff: it rests nothing beside orders it did not place)
            why["your orders there"] += 1
            r["verdict"] = "your orders there"
        elif pick["proj"] < a.bar and not same:
            why["under bar"] += 1
            r["verdict"] = "under bar"
        elif (cur or 0.0) >= pick["usd_day"] and not a.include_quoted:
            why["bot earns more"] += 1
            r["verdict"] = "bot earns more"
        else:
            r["verdict"] = "LIST"
        rows.append(r)
    take = [r for r in rows if r["verdict"] == "LIST"]
    kept = {t: e for t, e in listed_now.items() if t in unread and keep(t)
            and ts(e.get("stop") or "1970-01-01T00:00:00Z") > now and t not in {r["ticker"] for r in take}}
    log(f"cheap_scan: {len(progs)} program markets in {names}; "
        f"{len(far)} at {a.min_days:.0f}d+; listing {len(take)} "
        f"({sum(1 for r in take if r['same_period'])} already this period); unread {len(unread)} "
        f"(kept {len(kept)} listed); skipped {why}")
    print(f"{'ticker':36} {'sub':24} {'side':4} {'px':>2} {'size':>6} {'share':>6} {'$/day':>6} "
          f"{'risk$':>6} {'proj$':>6} {'now$/d':>6} stop")
    for r in sorted(rows, key=lambda r: -((r["pick"] or {}).get("usd_day") or 0)):
        p = r["pick"] or {}
        if not p:
            continue
        print(f"{r['ticker'][:36]:36} {r['sub'][:24]:24} {p['side'].upper():4} {p['px']:2d} {p['size']:6d} "
              f"{p['share'] * 100:5.0f}% {p['usd_day']:6.2f} {p['risk']:6.0f} {p['proj']:6.2f} "
              f"{r['bot_usd_day_now']:6.2f} {r['stop'][5:16]}  {'' if r['verdict'] == 'LIST' else r['verdict']}")
    log(f"TOTAL listed: ${sum(r['pick']['usd_day'] for r in take):,.2f}/day modelled, "
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
        L = {t: e for t, e in (cur.get("markets") or {}).items() if not keep(t)}
        L.update(kept)
        stamp = now.strftime("%Y-%m-%dT%H:%MZ")
        for r in take:
            p = r["pick"]
            old = listed_now.get(r["ticker"]) or {}
            e = {"side": p["side"], "px": p["px"], "size": p["size"], "stop": r["stop"],
                 "period_start": r["period_start"], "event": r["event"], "pool_day": r["pool_day"],
                 "est_usd_day": round(p["usd_day"], 2), "proj_period": round(p["proj"], 2),
                 "risk_usd": round(p["risk"], 2), "horizon": r["horizon"],
                 "added": f"cheap_scan {stamp} (share {p['share'] * 100:.0f}%)"}
            # an unchanged rung keeps its first listing note (the file only moves when a rung does)
            if all(old.get(k) == e[k] for k in ("side", "px", "size", "stop")):
                e = dict(old)
            L[r["ticker"]] = e
        before = {t: {k: v.get(k) for k in ("side", "px", "size", "stop")} for t, v in listed_now.items()}
        after = {t: {k: v.get(k) for k in ("side", "px", "size", "stop")} for t, v in L.items()}
        if before == after and os.path.exists(path):
            log(f"no rung changed: {path} left as is ({len(L)} listed)")
            return 0
        cur["markets"] = L
        cur["last_write"] = f"cheap_scan.py {stamp}: {len(take)} for {names}, max ${a.max_usd:g}"
        write_list(path, cur, after, now)
        added = sorted(set(after) - set(before))
        gone = sorted(set(before) - set(after))
        moved = sorted(t for t in set(after) & set(before) if after[t] != before[t])
        log(f"WROTE {len(L)} listed -> {path}: +{len(added)} {added[:8]} -{len(gone)} {gone[:8]} "
            f"changed {len(moved)} {moved[:8]}")
    else:
        print("dry run: nothing written")
    return 0


def write_list(path, cur, after, now) -> None:
    """Write the cheap-side list atomically, and append the listing to
    list_history.jsonl (the daily email's listed spans)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)
    try:   # every listing as written, for the daily email (send_cheap_report.py)
        with open(os.path.join(OUT_DIR, "list_history.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": zs(now), "markets": after}) + "\n")
    except OSError as e:
        log(f"! list_history.jsonl not appended ({e!r})")


def discover(a) -> int:
    """Score the liquidity-program markets OUTSIDE the list's series and write
    the series where a 1-2c rung would list to discover.json (for the email)."""
    now = datetime.now(timezone.utc)
    st, ok = load_series_state()
    prefixes, approved, keep = scope(a.series, st)
    rejected = dict(REJECTED_1108)
    rejected.update({s: (v or {}).get("why") or "rejected" for s, v in st["rejected"].items()})
    client = imm._fair_reader_client()
    progs = programs(client, lambda t: True, now)
    outside, n_rej = {}, 0
    for t, pr in progs.items():
        if keep(t):
            continue
        if pat_match(series_of(t), rejected):
            n_rej += 1
            continue
        outside[t] = pr
    tick = sorted(outside)
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
        if not ds or (min(ds) - now).total_seconds() / 86400 < a.min_days:
            continue
        yb = float(m.get("yes_bid_dollars") or 0)
        ya = float(m.get("yes_ask_dollars") or 0)
        # a 1-2c rung earns only near its side's best: a YES bid when the best
        # YES bid is within NEAR_BEST of 0, a NO bid when the best NO bid is
        if not (yb <= NEAR_BEST or ya <= 0 or ya >= 1 - NEAR_BEST):
            continue
        far[t] = (m, min(ds))
    if len(far) > MAX_DISCOVER_BOOKS:
        log(f"discover: {len(far)} candidate books, reading the {MAX_DISCOVER_BOOKS} biggest pools")
        far = {t: far[t] for t in sorted(far, key=lambda t: -outside[t]["pd"])[:MAX_DISCOVER_BOOKS]}
    own, own_exact, foreign = own_orders(client)

    def book(t):
        ob = rd(f"/markets/{t}/orderbook")
        time.sleep(0.2)
        return t, ob
    with ThreadPoolExecutor(4) as ex:
        books = dict(ex.map(book, sorted(far)))
    by, n_read = {}, 0
    for t, (m, hz) in sorted(far.items()):
        ob = books.get(t)
        if not ob:
            continue
        n_read += 1
        pr = outside[t]
        hrs = (pr["end"] - max(now, pr["start"])).total_seconds() / 3600
        pick, cur = best_rung(t, pr, ob, own.get(t, []), own_exact.get(t, []), a.max_usd, hrs)
        if pick is None or pick["proj"] < a.bar:
            continue
        g = by.setdefault(series_of(t), {"markets": [], "yours": 0, "bot_more": 0})
        if t in foreign:
            g["yours"] += 1             # the manual standoff: a rung would never rest there
        elif (cur or 0.0) >= pick["usd_day"]:
            g["bot_more"] += 1          # the bot earns more there under its family rules
        else:
            g["markets"].append({
                "ticker": t, "event": m.get("event_ticker"), "sub": (m.get("yes_sub_title") or "")[:40],
                "title": (m.get("title") or "")[:90], "side": pick["side"], "px": pick["px"],
                "size": pick["size"], "share": round(float(pick["share"]), 4),
                "usd_day": round(pick["usd_day"], 2), "risk": round(pick["risk"], 2),
                "proj": round(pick["proj"], 2), "horizon": zs(hz), "stop": zs(pr["end"]),
                # Kalshi's own words on an early close: every market says it can, so
                # only the wording helps Jack judge whether it can settle tomorrow
                "early": " ".join(str(m.get("early_close_condition") or "").split())[:160]})
    cands = []
    for s, g in sorted(by.items()):
        ms = sorted(g["markets"], key=lambda x: -x["usd_day"])
        if not ms:
            continue
        sm = (rd(f"/series/{s}") or {}).get("series") or {}
        seen = st["seen"].get(s) or {}
        cands.append({
            "series": s, "title": (sm.get("title") or ms[0]["title"] or "")[:90],
            "category": sm.get("category") or "", "first_seen": seen.get("first_seen") or zs(now),
            "new": not seen.get("emailed"), "n_list": len(ms), "yours": g["yours"],
            "bot_more": g["bot_more"], "events": sorted({x["event"] for x in ms if x["event"]}),
            "usd_day": round(sum(x["usd_day"] for x in ms), 2), "risk": round(sum(x["risk"] for x in ms), 2),
            "proj": round(sum(x["proj"] for x in ms), 2),
            "horizon_min": min(x["horizon"] for x in ms), "horizon_max": max(x["horizon"] for x in ms),
            "program_end": min(x["stop"] for x in ms),
            "early": next((x["early"] for x in ms if x.get("early")), ""), "top": ms[:5]})
    cands.sort(key=lambda c: (not c["new"], -c["usd_day"]))
    if ok:
        try:   # re-read: an --approve / --reject since the start stays
            st2, _ = load_series_state(strict=True)
            for c in cands:
                e = st2["seen"].setdefault(c["series"], {"first_seen": c["first_seen"]})
                e.update(last_seen=zs(now), usd_day=c["usd_day"], n_list=c["n_list"], title=c["title"])
            save_series_state(st2)
        except (OSError, ValueError) as e:
            log(f"! {SERIES_FILE} not updated ({e!r})")
    res = {"at": zs(now), "bar": a.bar, "max_usd": a.max_usd, "min_days": a.min_days,
           "programs": len(progs), "outside": len(outside), "rejected_markets": n_rej,
           "far_near_best": len(far), "books": n_read, "watching": list(prefixes) + sorted(approved),
           "rejected": sorted(rejected), "candidates": cands}
    tmp = DISCOVER_OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(res, fh, indent=1)
    os.replace(tmp, DISCOVER_OUT)
    log(f"discover: {len(progs)} program markets, {len(outside)} outside the list's series "
        f"({n_rej} in rejected series), {len(far)} far with a side near 0, {n_read} books read; "
        f"{len(cands)} series would list ({sum(1 for c in cands if c['new'])} new): "
        + (", ".join(f"{c['series']} {c['n_list']} ${c['usd_day']:.2f}/d" for c in cands[:12]) or "none"))
    return 0


def mark_emailed(series, day: str) -> None:
    """The daily email showed these series (send_cheap_report.py, after a
    send): they are NEW no more."""
    if not series:
        return
    try:
        st, _ = load_series_state(strict=True)
        for s in series:
            st["seen"].setdefault(s, {"first_seen": day}).setdefault("emailed", day)
        save_series_state(st)
    except (OSError, ValueError) as e:
        log(f"! {SERIES_FILE} not marked emailed ({e!r})")


def decide(a) -> int:
    """--approve / --reject: Jack's call on a series."""
    now = datetime.now(timezone.utc)
    if a.approve and a.reject:
        log("! --approve and --reject together: one at a time")
        return 1
    try:
        st, _ = load_series_state(strict=True)
    except (OSError, ValueError) as e:
        log(f"! {SERIES_FILE} unreadable ({e!r}): fix or remove it first")
        return 1
    names = [s.strip().upper() for s in (a.approve or a.reject).split(",") if s.strip()]
    for s in names:
        if a.approve:
            st["approved"][s] = {"at": zs(now), "note": a.why}
            st["rejected"].pop(s, None)
            p = pat_match(s, REJECTED_1108)
            log(f"approved {s} for the cheap-side books"
                + (f" (over the 11/08 review: {REJECTED_1108[p]})" if p else ""))
        else:
            st["rejected"][s] = {"at": zs(now), "why": a.why}
            st["approved"].pop(s, None)
            log(f"rejected {s}" + (f": {a.why}" if a.why else ""))
    save_series_state(st)
    if a.reject:
        path = imm.cheap_path()
        try:
            with open(path, encoding="utf-8") as fh:
                cur = json.load(fh) or {}
        except (OSError, ValueError):
            cur = {}
        L = cur.get("markets") or {}
        gone = sorted(t for t in L if series_of(t) in names)
        if gone:
            cur["markets"] = {t: e for t, e in L.items() if t not in gone}
            cur["last_write"] = f"cheap_scan.py --reject {now.strftime('%Y-%m-%dT%H:%MZ')}: {','.join(names)}"
            after = {t: {k: v.get(k) for k in ("side", "px", "size", "stop")} for t, v in cur["markets"].items()}
            write_list(path, cur, after, now)
            log(f"delisted {len(gone)}: {gone[:8]}")
        return 0
    if a.write:
        return scan(a)
    log("the hourly scan (:25) lists its paying markets; --write lists them now")
    return 0


def render_discover(d, now):
    """(lines, n_new): the daily email's new-series section from a
    discover.json dict (None: the check did not run)."""
    if not d:
        return (["", "NEW FAR-DATED SERIES: the daily check did not run "
                     "(run-logs/cheap-scan/discover.json missing)."], 0)
    cands = d.get("candidates") or []
    new = [c for c in cands if c.get("new")]
    old = [c for c in cands if not c.get("new")]
    age_h = (now - ts(d["at"])).total_seconds() / 3600
    head = (f"NEW FAR-DATED SERIES FOR YOU TO APPROVE: {len(new)} new, {len(old)} still waiting (checked "
            f"{d['at'][:16].replace('T', ' ')}Z: {d.get('books', 0):,} far books outside the list's "
            f"{len(d.get('watching') or [])} series)")
    if age_h > 26:
        head += f" -- STALE, {age_h:.0f}h old"
    lines = ["", head + ":"]
    if not cands:
        lines.append(f"  none: no other series has a far-dated market where a 1-2c rung projects "
                     f"${d.get('bar', 1.1):.2f} by its program end at ${d.get('max_usd', 50):g} a market.")
    for c in new:
        hz = c["horizon_min"][:10] + (f" to {c['horizon_max'][:10]}" if c["horizon_max"][:10] != c["horizon_min"][:10]
                                      else "")
        more = "".join([f"; {c['yours']} more with your orders there" if c.get("yours") else "",
                        f"; {c['bot_more']} more the bot already earns on" if c.get("bot_more") else ""])
        lines.append(f"  NEW {c['series']} -- {c['title']} [{c['category'] or '?'}]")
        lines.append(f"      {c['n_list']} market(s) pay: ${c['usd_day']:.2f}/day modelled, ${c['risk']:,.0f} if "
                     f"every rung fills, ${c['proj']:.2f} by the program end {c['program_end'][:10]}; Kalshi "
                     f"dates it {hz}{more}")
        if c.get("early"):
            lines.append(f"      Kalshi on an early close: \"{c['early']}\"")
        for x in c["top"][:3]:
            lines.append(f"      {x['ticker']} ({x['sub']}): {x['side'].upper()} bid {x['px']}c x{x['size']:,}, "
                         f"{x['share'] * 100:.0f}% of its side, ${x['usd_day']:.2f}/day")
    for c in old[:10]:
        lines.append(f"  waiting since {c['first_seen'][:10]}: {c['series']} -- {c['title'][:60]}: "
                     f"{c['n_list']} pay ${c['usd_day']:.2f}/day")
    if len(old) > 10:
        lines.append(f"  ... and {len(old) - 10} more (run-logs/cheap-scan/discover.json)")
    if cands:
        lines.append("  Could it settle tomorrow -- a proof, an announcement, a signing, a transfer? Then it "
                     "is not truly far (your 11/08 call on KXPNP, P vs NP before 2028). Kalshi's dates do not "
                     "tell (Grammy nominations read 358 days out, announced 11/16), nor does its early-close "
                     "flag (every market carries it). To quote one, tell Claude \"approve <series> for the "
                     "cheap-side books\"; to stop seeing it, \"reject <series>\" (cheap_scan.py --approve / "
                     "--reject).")
    return lines, len(new)


if __name__ == "__main__":
    sys.exit(main())
