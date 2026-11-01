#!/usr/bin/env python3
"""Carbon Arc settled-strike DRY RUN (Jack 2026-11-01: "Yes, both", on "Ship
the landing-window fix now and build the settled-strike mode as a dry run?").

Most of Kalshi's Carbon Arc reward pool pays while the bot is out. 61% of the
$387k paid on October's events came after 10/17: $114k in the late window
(10/18-10/26, where we earned $91) and $122k after the 10/27 stop through the
prints (where we earned nothing). For September's events it was 98% of $154k,
all after the 9/26 stop. Our read settles most of those strikes. September's
final reads matched Kalshi's prints exactly for ad spend, apps and foot traffic
(34 of 34 series within 0.04). Point of sale was within 1.1 (C4 printed 14.7
over its read: a restatement before the print). Card spend was 0.4 under at
the median and 2.5 at worst, because its feed rolls to the next month before
it shows the last 2-3 days.

This module only WATCHES. It picks every Carbon Arc brand market with an
active program whose strike our read settles: |read - strike| at least the
margin below, with the market agreeing. For each, it reads the book and prices
what the mode would rest:
  - on the settled side, a bid joining that side's best bid;
  - on the other side, a bid joining its best bid only at WRONG_MAX_C or less,
    else 1c, so a fill there costs at most that.
It scores those orders with incentive_mm's own reward model
(estimate_reward_share) at each SIZES pair, and logs one line per market to
ca_settled_dry_<UTC date>.jsonl. Nothing is placed.

`python ca_settled_dry.py` runs one pass and prints its summary. The family
watch runs one every 30 minutes (pass 8).
`python ca_settled_dry.py --report [--days N]` scores the logs: the reward
estimate over time, plus the fills the public tape would have given us. Those
are an upper bound, since every trade at or through our price fills us first.
They are valued at Kalshi's result once settled, else at the read.
"""
import argparse
import glob
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

import imm_quote_gaps  # noqa: F401  (launcher env, then incentive_mm)
import incentive_mm as imm
import carbon_arc_fair as caf

API = os.environ.get("IMM_CA_SETTLED_API", "https://api.elections.kalshi.com/trade-api/v2")
LOG_GLOB = "ca_settled_dry_20??-??-??.jsonl"
PASS_SECS = 1800                     # the family watch's cadence
WORKERS = int(os.environ.get("IMM_CA_SETTLED_WORKERS", "4"))
# The settled margin, in index points. The floor is per category: card spend
# is wider because its feed never shows the month's last 2-3 days, POS because
# of C4's restatement. On top of it: MARGIN_UNOBS_K x the unobserved share of
# the month (1 - w) x the month-to-month spread (sigma_m). At least MARGIN_REL
# of the read (KXCLAUDEAPP reads ~1,900).
MARGIN_FLOOR = {"advertising": 3.0, "app": 3.0, "foot traffic": 3.0,
                "point of sale": 4.0, "credit card": 5.0}
MARGIN_UNOBS_K = 3.0
MARGIN_REL = 0.03
# November: Carbon Arc's YoY compares the same dates, and Nov 2026 holds Cyber
# Monday (11/30) where Nov 2025's fell on 12/1 (Adobe: $14.25B online, 3-4x
# an ordinary day). So online-heavy card names and ad spend print above the
# month-to-date we see at the stop. Wider margins for card and ad spend; the
# online-heavy card names -- Amazon, Temu, Shein and the buy-now-pay-later
# lenders (Adobe: BNPL passed $1B on Cyber Monday 2025) -- sit out after the
# month ends, when card's unseen last days are Sunday 11/29 and Cyber Monday.
HOLIDAY_EXTRA = {"11": {"credit card": 3.0, "advertising": 3.0}}
HOLIDAY_SKIP_AFTER_MONTH_END = {"11": frozenset({"KXAMZNCC", "KXTEMUCC", "KXSHEINCC", "KXAFRMCC",
                                                 "KXAFTERPAYCC", "KXKLARCC"})}
AGREE_C = 20        # a bid over this for the outcome our read rules out = the market disagrees
WRONG_MAX_C = 3     # the other side joins its best bid only at this price or under, else 1c
SIZES = ((20, 20), (20, 1000), (150, 1000))   # (settled side, other side) contracts
_STRIKE_RE = re.compile(r"-T(-?\d+(?:\.\d+)?)$")


def measurement_month(ticker: str) -> Optional[str]:
    """'YYYY-MM' a Carbon Arc market measures: the month before its print-
    date code (KXAMZNCC-26NOV07 -> 2026-10)."""
    me = imm.ca_month_end_utc(ticker)
    if me is None:
        return None
    return (me.astimezone(imm.ET) - timedelta(days=1)).strftime("%Y-%m")


def strike_of(ticker: str) -> Optional[float]:
    m = _STRIKE_RE.search(ticker)
    return float(m.group(1)) if m else None


def load_reads(fair_path: Optional[str] = None,
               vintage_path: Optional[str] = None) -> Dict[Tuple[str, str], dict]:
    """(series, 'YYYY-MM') -> our newest read of that month: the vintage log's
    last row, or the fair file's entry when it is newer. After a category
    rolls to the next month, the fair file no longer holds the old month; the
    vintage log keeps its final read."""
    reads: Dict[Tuple[str, str], dict] = {}

    def keep(series: str, r: dict) -> None:
        key = (series, str(r.get("month") or ""))
        old = reads.get(key)
        rank = (str(r.get("data_through") or ""), str(r.get("fetched_at") or ""))
        if old is None or rank >= (str(old.get("data_through") or ""),
                                   str(old.get("fetched_at") or "")):
            reads[key] = dict(r, series=series)

    try:
        with open(vintage_path or caf.CA_VINTAGE_FILE, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if r.get("series") and r.get("month"):
                    keep(r["series"], r)
    except OSError:
        pass
    try:
        with open(fair_path or imm.CA_FAIR_FILE, encoding="utf-8") as f:
            entries = (json.load(f) or {}).get("entries") or {}
    except (OSError, ValueError):
        entries = {}
    for s, e in entries.items():
        if isinstance(e, dict) and e.get("month"):
            keep(s, e)
    return reads


def margin(read: dict, series: str, month: str, after_month_end: bool) -> Optional[float]:
    """The distance from the read a strike must sit to count as settled, or
    None when the series sits the mode out."""
    cat = str(read.get("category") or "").strip().lower()
    if cat not in MARGIN_FLOOR:
        return None
    mm = month[5:7]
    if after_month_end and series in HOLIDAY_SKIP_AFTER_MONTH_END.get(mm, ()):
        return None
    try:
        mu = float(read["mtd"])
        w = float(read.get("w") or 0.0)
        sm = float(read.get("sigma_m") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    m = (MARGIN_FLOOR[cat] + MARGIN_UNOBS_K * max(0.0, 1.0 - w) * sm
         + HOLIDAY_EXTRA.get(mm, {}).get(cat, 0.0))
    return max(m, MARGIN_REL * mu)


def settled_side(mu: float, strike: float, need: float) -> Optional[str]:
    """'yes' / 'no' when the read clears a "greater" strike by `need`."""
    if mu - strike >= need:
        return "yes"
    if strike - mu >= need:
        return "no"
    return None


def parse_book(j: dict) -> Tuple[List[List[float]], List[List[float]]]:
    """(yes bids, no bids) as [[cents, size], ...] from a public orderbook
    reply: "orderbook_fp" (dollar strings, since ~10/2026), else "orderbook"."""
    fp = j.get("orderbook_fp")
    if fp:
        conv = lambda rows: [[int(round(float(p) * 100)), float(q)] for p, q in rows or []]
        return conv(fp.get("yes_dollars")), conv(fp.get("no_dollars"))
    ob = j.get("orderbook") or {}
    conv = lambda rows: [[int(p), float(q)] for p, q in rows or []]
    return conv(ob.get("yes")), conv(ob.get("no"))


def proposal(yes: List[List[float]], no: List[List[float]], side: str,
             target: float, df: float, sizes=SIZES) -> dict:
    """What the mode would rest on a settled strike, and its reward share at
    each size. `status` is "ok", "disagree" (someone bids more than AGREE_C
    for the outcome our read rules out: real money says it is live. A mid
    would also drop the 1c-placeholder books, whose 50c mid says nothing),
    or "empty" (no bid on the settled side to join)."""
    ybid = max((p for p, _ in yes), default=None)
    nbid = max((p for p, _ in no), default=None)
    right = ybid if side == "yes" else nbid
    wrong = nbid if side == "yes" else ybid
    out = {"right_px": right, "wrong_px": None, "mid": None, "est": {}}
    if ybid is not None and nbid is not None:
        mid_yes = (ybid + 100 - nbid) / 2.0
        out["mid"] = round(mid_yes if side == "yes" else 100 - mid_yes, 1)
    if wrong is not None and wrong > AGREE_C:
        return dict(out, status="disagree")
    if right is None:
        return dict(out, status="empty")
    wpx = wrong if (wrong is not None and wrong <= WRONG_MAX_C) else 1
    out["wrong_px"] = wpx
    for sr, sw in sizes:
        if side == "yes":     # YES bid at the best; NO bid at wpx = YES ask at 100 - wpx
            orders = [("bid", right, float(sr)), ("ask", 100 - wpx, float(sw))]
        else:                 # NO bid at the best = YES ask; YES bid at wpx
            orders = [("ask", 100 - right, float(sr)), ("bid", wpx, float(sw))]
        frac, _sides = imm.estimate_reward_share(yes, no, orders, target, df,
                                                 own_in_book=False)
        out["est"][f"{sr}/{sw}"] = round(frac, 6)
    return dict(out, status="ok")


def usd_per_day(p: dict) -> float:
    """A program row's pay per day (period_reward is in centi-cents)."""
    a = imm.parse_iso_utc(p.get("start_date", ""))
    b = imm.parse_iso_utc(p.get("end_date", ""))
    if not a or not b:
        return 0.0
    days = max((b - a).total_seconds() / 86400.0, 1.0 / 24)
    return (p.get("period_reward") or 0) / 10000.0 / days


_session = requests.Session()


def _get_json(path: str, params: Optional[dict] = None, tries: int = 6) -> dict:
    """GET a public market-data path (no key: the bot's rate budget is left
    alone), backing off on 429."""
    for i in range(tries):
        try:
            r = _session.get(API + path, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1.0 + i)
                continue
            r.raise_for_status()
            return r.json() or {}
        except requests.RequestException:
            if i == tries - 1:
                raise
            time.sleep(1.0 + i)
    return {}


def fetch_books(tickers: List[str]) -> Dict[str, Tuple[list, list]]:
    def one(t):
        try:
            return t, parse_book(_get_json(f"/markets/{t}/orderbook"))
        except Exception:                                # noqa: BLE001 - counted as missing
            return t, None
    with ThreadPoolExecutor(max(1, WORKERS)) as ex:
        return {t: b for t, b in ex.map(one, tickers) if b is not None}


def log_path_for(now: datetime) -> str:
    return os.path.join(imm.STATUS_DIR, f"ca_settled_dry_{now.strftime('%Y-%m-%d')}.jsonl")


def run_pass(progs: Dict[str, dict], now: Optional[datetime] = None,
             dry: bool = False, reads: Optional[Dict[Tuple[str, str], dict]] = None,
             books: Optional[Callable[[List[str]], Dict[str, tuple]]] = None,
             path: Optional[str] = None) -> dict:
    """One dry-run pass over `progs` (market ticker -> active program row, as
    imm_feed_audit.fetch_active_programs gives). Appends one line per settled
    strike to the day's log unless `dry`; returns the pass's counts and
    estimated $/day."""
    now = now or datetime.now(timezone.utc)
    reads = load_reads() if reads is None else reads
    skip: Dict[str, int] = defaultdict(int)
    cands = []
    for t, p in progs.items():
        s = imm.series_of(t)
        if not imm.carbon_arc_settled(s) or s.endswith("ARRAY"):
            continue
        a = imm.parse_iso_utc(p.get("start_date", ""))
        b = imm.parse_iso_utc(p.get("end_date", ""))
        if not a or not b or not (a <= now < b):
            skip["program not running"] += 1
            continue
        month, k = measurement_month(t), strike_of(t)
        r = reads.get((s, month)) if month else None
        if k is None or r is None:
            skip["no strike or read"] += 1
            continue
        try:
            mu = float(r["mtd"])
        except (KeyError, TypeError, ValueError):
            mu = 0.0
        if mu <= 0:                       # a vendor dropout (KXCVXCC read 0.0 on 10/27-29)
            skip["read is zero"] += 1
            continue
        need = margin(r, s, month, now >= imm.ca_month_end_utc(t))
        if need is None:
            skip["series sits out"] += 1
            continue
        side = settled_side(mu, k, need)
        if side is None:
            skip["near the money"] += 1
            continue
        cands.append((t, s, p, r, k, need, side, mu))
    got = (books or fetch_books)([c[0] for c in cands])
    rows, est_usd, pool = [], defaultdict(float), 0.0
    for t, s, p, r, k, need, side, mu in cands:
        bk = got.get(t)
        if bk is None:
            skip["book unread"] += 1
            continue
        target = float(p.get("target_size_fp") or 0)
        df = (p.get("discount_factor_bps") or 5000) / 10000.0
        prop = proposal(bk[0], bk[1], side, target, df)
        if prop["status"] != "ok":
            skip[prop["status"]] += 1
            continue
        usd = usd_per_day(p)
        pool += usd
        for key, frac in prop["est"].items():
            est_usd[key] += frac * usd
        rows.append({"ts": round(now.timestamp()), "t": t, "cat": str(r.get("category") or ""),
                     "month": r.get("month"), "mu": round(mu, 2), "thru": r.get("data_through"),
                     "k": k, "need": round(need, 2), "side": side, "mid": prop["mid"],
                     "rpx": prop["right_px"], "wpx": prop["wrong_px"], "usd": round(usd, 3),
                     "est": prop["est"]})
    if rows and not dry:
        with open(path or log_path_for(now), "a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
    return {"strikes": len(rows), "pool": pool, "est": dict(est_usd), "skip": dict(skip)}


def summary_line(res: dict) -> str:
    est = res.get("est") or {}
    return (f"ca settled (dry) {res.get('strikes', 0)} strikes, pool ${res.get('pool', 0):,.0f}/day, est "
            + " / ".join(f"${est.get(f'{a}/{b}', 0):,.0f}" for a, b in SIZES)
            + " a day at " + ", ".join(f"{a}/{b}" for a, b in SIZES))


# ---- report -----------------------------------------------------------------

def read_logs(since: datetime) -> Dict[str, List[dict]]:
    """ticker -> its dry-run rows since `since`, oldest first."""
    out: Dict[str, List[dict]] = defaultdict(list)
    for path in sorted(glob.glob(os.path.join(imm.STATUS_DIR, LOG_GLOB))):
        day = os.path.basename(path)[len("ca_settled_dry_"):-len(".jsonl")]
        if day < (since - timedelta(days=1)).strftime("%Y-%m-%d"):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("ts", 0) >= since.timestamp():
                    out[row["t"]].append(row)
    for rows in out.values():
        rows.sort(key=lambda x: x["ts"])
    return out


def fetch_trades(ticker: str, a_ts: int, b_ts: int) -> List[dict]:
    """The public tape for one market in [a_ts, b_ts]: (ts, taker_side,
    yes_px cents, no_px cents, count)."""
    out, cursor = [], None
    for _ in range(50):
        params = {"ticker": ticker, "min_ts": int(a_ts), "max_ts": int(b_ts), "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        j = _get_json("/markets/trades", params)
        for tr in j.get("trades") or []:
            ts = imm.parse_iso_utc(tr.get("created_time", ""))
            yp = tr.get("yes_price_dollars")
            np_ = tr.get("no_price_dollars")
            out.append({"ts": ts.timestamp() if ts else 0.0,
                        "taker": tr.get("taker_side"),
                        "yes": int(round(float(yp) * 100)) if yp is not None else int(tr.get("yes_price") or 0),
                        "no": int(round(float(np_) * 100)) if np_ is not None else int(tr.get("no_price") or 0),
                        "n": float(tr.get("count_fp") or tr.get("count") or 0)})
        cursor = j.get("cursor")
        if not cursor or not j.get("trades"):
            break
    return out


def fills_for(rows: List[dict], trades: List[dict], size_key: str, won: bool) -> dict:
    """The fills the tape would have given the rows' orders at `size_key`
    ("settled side/other side" contracts) and what they make once the market
    settles (`won`: the settled side won). An upper bound: each trade at or
    through our price fills us first, up to the order's size, for as long as
    that row stands (PASS_SECS, or until the next row)."""
    sr, sw = (float(x) for x in size_key.split("/"))
    out = {"right": 0.0, "wrong": 0.0, "pnl": 0.0}

    def fill(q: float, px: float, settled_side: bool) -> None:
        # px: cents paid for the contract bought (the settled side, or the other)
        out["right" if settled_side else "wrong"] += q
        pays = won if settled_side else not won
        out["pnl"] += q * ((100 - px) if pays else -px) / 100.0

    for i, row in enumerate(rows):
        a = row["ts"]
        b = min(rows[i + 1]["ts"] if i + 1 < len(rows) else a + PASS_SECS, a + PASS_SECS)
        left = {True: sr, False: sw}
        # our YES bid / NO bid prices this row (settled side at rpx, other at wpx)
        yes_px = row["rpx"] if row["side"] == "yes" else row["wpx"]
        no_px = row["wpx"] if row["side"] == "yes" else row["rpx"]
        for tr in trades:
            if not (a <= tr["ts"] < b):
                continue
            # a "no" taker sells YES into the YES bids at or above its price;
            # a "yes" taker sells NO into the NO bids at or above its price
            if tr["taker"] == "no" and tr["yes"] <= yes_px:
                ours = row["side"] == "yes"
                q = min(tr["n"], left[ours]); left[ours] -= q
                fill(q, yes_px, ours)
            elif tr["taker"] == "yes" and tr["no"] <= no_px:
                ours = row["side"] == "no"
                q = min(tr["n"], left[ours]); left[ours] -= q
                fill(q, no_px, ours)
    return out


def score_market(t: str, rows: List[dict], trades_of, result_of) -> dict:
    """One market's dry run: reward estimate and tape fills at each size."""
    keys = [f"{a}/{b}" for a, b in SIZES]
    out = {"cat": rows[-1]["cat"] or "?", "result": None}
    for i, row in enumerate(rows):
        nxt = rows[i + 1]["ts"] if i + 1 < len(rows) else row["ts"] + PASS_SECS
        dt = min(nxt - row["ts"], PASS_SECS)
        for k in keys:
            out[f"rew {k}"] = out.get(f"rew {k}", 0.0) + row["est"].get(k, 0.0) * row["usd"] * dt / 86400.0
    res = result_of(t)
    out["result"] = res if res in ("yes", "no") else None
    won = (res == rows[-1]["side"]) if out["result"] else True     # unsettled: at the read
    out["won"] = won
    trades = trades_of(t, rows[0]["ts"], rows[-1]["ts"] + PASS_SECS)
    for k in keys:
        f = fills_for(rows, trades, k, won)
        out[f"fill_r {k}"], out[f"fill_w {k}"], out[f"pnl {k}"] = f["right"], f["wrong"], f["pnl"]
    return out


def report(days: float = 7.0, now: Optional[datetime] = None,
           trades_of: Callable[[str, int, int], List[dict]] = fetch_trades,
           result_of: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """The dry run so far, by category: estimated reward, the fills the tape
    would have given us and what they made or lost, at each size."""
    now = now or datetime.now(timezone.utc)
    logs = read_logs(now - timedelta(days=days))
    if not logs:
        return "ca settled dry run: no logged strikes yet"
    result_of = result_of or (lambda t: (_get_json(f"/markets/{t}").get("market") or {}).get("result") or None)
    keys = [f"{a}/{b}" for a, b in SIZES]
    agg = defaultdict(lambda: defaultdict(float))
    against = []
    with ThreadPoolExecutor(max(1, WORKERS)) as ex:
        scored = list(ex.map(lambda kv: (kv[0], kv[1], score_market(kv[0], kv[1], trades_of, result_of)),
                             sorted(logs.items())))
    for t, rows, m in scored:
        a = agg[m["cat"]]
        a["strikes"] += 1
        if m["result"]:
            a["settled"] += 1
            if not m["won"]:
                against.append(f"{t} read {rows[-1]['mu']} vs strike {rows[-1]['k']}, settled {m['result']}")
        for kk, v in m.items():
            if kk[:4] in ("rew ", "fill", "pnl "):
                a[kk] += v
    lines = [f"Carbon Arc settled-strike DRY RUN, last {days:g} days "
             f"({sum(len(r) for r in logs.values()):,} logged rows, {len(logs)} strikes)",
             "per size (settled side / other side): est reward | fills settled / other side (ct) "
             "| fill P&L | net. Fills are an upper bound (first in queue)."]
    tot = defaultdict(float)
    for cat, a in sorted(agg.items()):
        for kk, v in a.items():
            tot[kk] += v
        lines.append(f"  {cat} ({a['strikes']:.0f} strikes, {a['settled']:.0f} settled)")
        for k in keys:
            net = a[f"rew {k}"] + a[f"pnl {k}"]
            lines.append(f"     {k:>9}: ${a[f'rew {k}']:8,.2f} | {a[f'fill_r {k}']:7,.0f} / "
                         f"{a[f'fill_w {k}']:7,.0f} | ${a[f'pnl {k}']:+9,.2f} | ${net:+9,.2f}")
    lines.append("  ALL")
    for k in keys:
        net = tot[f"rew {k}"] + tot[f"pnl {k}"]
        lines.append(f"     {k:>9}: ${tot[f'rew {k}']:8,.2f} | {tot[f'fill_r {k}']:7,.0f} / "
                     f"{tot[f'fill_w {k}']:7,.0f} | ${tot[f'pnl {k}']:+9,.2f} | ${net:+9,.2f}")
    if against:
        lines.append(f"  settled AGAINST the read ({len(against)}):")
        lines.extend(f"     {x}" for x in against[:20])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--report", action="store_true", help="score the dry-run logs")
    ap.add_argument("--days", type=float, default=7.0)
    ap.add_argument("--dry", action="store_true", help="a pass that writes nothing")
    args = ap.parse_args(argv)
    if args.report:
        print(report(args.days))
        return 0
    import imm_feed_audit as ifa
    res = run_pass(ifa.fetch_active_programs(imm.build_client()), dry=args.dry)
    print(summary_line(res))
    print("skipped:", res["skip"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
