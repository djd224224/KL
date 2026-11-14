"""The main account's cash through the day, for the IMM dashboard's Cash
section (Jack 2026-11-14: "add a time series chart view of cash balance and how
it's changing throughout the day e.g. what families are drawing cash or
adding cash, rewards, deposits, etc").

Readings. The cash balance itself (balance_dollars: the primary account,
every shard; the numbered subaccounts hold their own) as Kalshi reported it:
  - each 7am portfolio email's snapshot (portfolio_daily/pf_snapshot_<day>:
    cash, the collateral Kalshi has handed back on open positions, the
    all-time deposited / withdrawn totals), and
  - from 2026-11-14 every dashboard build's live account read (account_now,
    every ~10 minutes), kept in <dashboard>/cash_samples.jsonl.

Moves between readings. Every account fill and settlement, replayed from the
start snapshot's positions exactly as the email replays its day
(send_portfolio_digest.replay_day, flows): a fill that opens contracts pays
cash out ("buy"), one that closes or nets them pays cash in ("sell"), a
settlement pays its revenue ("settle"). Fees ride on the fill.
  - A fill is the IMM's when its Kalshi fill id is in the bot's own fills
    log, and goes to its family as Drivers files it. The rest go to the
    crypto bots, the weather bots or "other trades" (hand trades, the other
    bots) by series. A settlement is the IMM family's when the bot traded
    the market.

What no record explains, reading to reading, in this order:
  - "transfer": deposits and withdrawals (the move in the all-time totals,
    placed at the transfer's own time when one inside the interval adds up
    to it) and moves between the primary and the subaccounts;
  - "collateral": the change in the collateral Kalshi hands back on open
    mutually exclusive and nested positions (no fill behind it);
  - "reward": the rest. Almost all of it is Kalshi's reward batch (~6:15 AM
    ET); anything else with no record (a perps transfer) lands here too.
These land on the reading that shows them. So between two 7am readings (all
days before the live readings began) the collateral and the reward credits
of the whole day sit on the 7am reading; from then on, on the 10-minute read
that saw them. The path ties to every reading to the cent by construction.
"""
import json
import os
from datetime import datetime, timedelta, timezone

BUCKET = 600                        # 10-minute cells
KINDS = ("buy", "sell", "settle", "reward", "transfer", "collateral")
# a non-trade kind's source row (its own line in the table)
ACCOUNT_SRC = {"reward": "Reward credits", "transfer": "Deposits & transfers",
               "collateral": "Collateral (Kalshi)"}
BOT_SRC = {"Crypto": "Crypto bots", "Weather": "Weather bots", "Other": "Other trades"}
SAMPLES_FILE = "cash_samples.jsonl"
SEG_PREFIX = "cash_seg_"
SEG_VERSION = 1
# completed 7am -> 7am days replayed per build (each ~3 Kalshi fill pages);
# the newest first, so a cold start fills the recent days in one build
MAX_SEG_PER_BUILD = int(os.environ.get("IMM_DASH_CASH_SEGS", "10"))
SAMPLE_KEEP_DAYS = 40


_BOT_MEMO = {}


def bot_src(series: str) -> str:
    """The source row of a fill that is not the IMM's: the email's bot rule
    (send_portfolio_digest.bot_of_event: crypto assets, temperature dailies,
    else other) by series."""
    v = _BOT_MEMO.get(series)
    if v is None:
        import send_portfolio_digest as spd
        v = _BOT_MEMO[series] = BOT_SRC[spd.bot_of_event(series, None)]
    return v


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------------
# Readings
# ----------------------------------------------------------------------------

def snapshot_read(S: dict) -> dict:
    """A 7am snapshot as a reading."""
    return {"ts": _utc(S["created_utc"]).timestamp(), "cash": _f(S.get("cash")),
            "R": S.get("collateral_returned"), "dep": S.get("deposited"),
            "wd": S.get("withdrawn"), "src": "7am", "d": S.get("date")}


def live_read(pf: dict, at: float) -> dict:
    """build_portfolio's account read as a reading (with the cash per shard)."""
    sh = {}
    for r in ((pf.get("shards") or {}).get("rows") or []):
        sh[str(r.get("shard"))] = _f(r.get("cash"))
    return {"ts": float(at), "cash": _f(pf.get("cash")), "R": pf.get("collateral_returned"),
            "dep": pf.get("deposited"), "wd": pf.get("withdrawn"), "src": "live", "sh": sh}


def record_sample(dash_dir: str, read: dict, now: float) -> bool:
    """Append a live reading to cash_samples.jsonl unless it is already
    there (the account read is cached across builds). Trims readings older
    than SAMPLE_KEEP_DAYS once the oldest line is a day past it."""
    path = os.path.join(dash_dir, SAMPLES_FILE)
    rows = load_samples(dash_dir)
    if rows and rows[-1]["ts"] >= read["ts"] - 1:
        return False
    cut = now - SAMPLE_KEEP_DAYS * 86400
    os.makedirs(dash_dir, exist_ok=True)
    if rows and rows[0]["ts"] < cut - 86400:
        keep = [r for r in rows if r["ts"] >= cut] + [read]
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            for r in keep:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.replace(path + ".tmp", path)
    else:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(read, separators=(",", ":")) + "\n")
    return True


def load_samples(dash_dir: str) -> list:
    out = []
    try:
        with open(os.path.join(dash_dir, SAMPLES_FILE), encoding="utf-8") as f:
            for ln in f:
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue              # a torn last line
                if isinstance(r, dict) and r.get("ts") and r.get("cash") is not None:
                    out.append(r)
    except OSError:
        pass
    out.sort(key=lambda r: r["ts"])
    return out


# ----------------------------------------------------------------------------
# Moves
# ----------------------------------------------------------------------------

def replay_flows(spd, S0: dict, fills: list, setts: list, t1: float, S1: dict = None):
    """(flows, mismatches): the cash moves from snapshot S0 to t1, every fill
    and settlement in (S0's time, t1], replayed from S0's positions."""
    t0 = _utc(S0["created_utc"]).timestamp()
    start, ev_map = spd.start_values(S0.get("tickers"), "mid")
    fs = [x for x in fills if int(t0) < int(x.get("ts") or 0) <= int(t1)]
    ss = []
    for s in setts:
        try:
            ts = datetime.fromisoformat((s.get("settled_time") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if t0 < ts <= t1:
            ss.append(s)
    end = {}
    if S1 is not None:
        for _ev, tks in (S1.get("tickers") or {}).items():
            for tk, r in tks.items():
                end[tk] = (_f(r.get("pos")), 0.0)
    flows = []
    _evs, _cash, mism, _settled = spd.replay_day(
        start, fs, ss, end, lambda tk: ev_map.get(tk) or spd.event_from_ticker(tk), flows=flows)
    return [[float(a), tk, k, round(v, 4), fid] for a, tk, k, v, fid in flows], (
        len(mism) if S1 is not None else None), len(fs), len(ss)


def seg_path(cache_dir: str, d0: str) -> str:
    return os.path.join(cache_dir, f"{SEG_PREFIX}{d0}.json")


def load_seg(cache_dir: str, d0: str, d1: str):
    try:
        with open(seg_path(cache_dir, d0), encoding="utf-8") as f:
            seg = json.load(f)
    except (OSError, ValueError):
        return None
    if seg.get("v") != SEG_VERSION or seg.get("d1") != d1:
        return None                     # the next snapshot changed (re-recorded)
    return seg


def compute_segs(spd, client, pairs, cache_dir: str, log=print) -> list:
    """Replay each completed day (S0 -> S1, consecutive 7am snapshots) with
    one fills read and one settlements read over all of them; cache each."""
    if not pairs:
        return []
    t_lo = min(_utc(a["created_utc"]) for a, _b in pairs)
    t_hi = max(_utc(b["created_utc"]) for _a, b in pairs)
    fills = spd.fetch_fills(client, t_lo, t_hi)
    setts = spd.fetch_recent_settlements(client, t_lo - timedelta(hours=1))
    out = []
    os.makedirs(cache_dir, exist_ok=True)
    for S0, S1 in pairs:
        t1 = _utc(S1["created_utc"]).timestamp()
        flows, mism, nf, ns = replay_flows(spd, S0, fills, setts, t1, S1)
        seg = {"v": SEG_VERSION, "d0": S0["date"], "d1": S1["date"],
               "a0": snapshot_read(S0), "a1": snapshot_read(S1), "flows": flows,
               "fills": nf, "setts": ns, "mism": mism}
        p = seg_path(cache_dir, S0["date"])
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            json.dump(seg, f, separators=(",", ":"))
        os.replace(p + ".tmp", p)
        log(f"cash: replayed {S0['date']} -> {S1['date']}: {nf} fills, {ns} settlements, "
            f"{mism} mismatches, trade cash {sum(x[3] for x in flows):+,.2f}")
        out.append(seg)
    return out


# ----------------------------------------------------------------------------
# What no record explains
# ----------------------------------------------------------------------------

def gap_items(a: dict, b: dict, flow_sum: float, transfers, moves) -> list:
    """[(ts, kind, $)] that close the interval a -> b: the transfers, the
    collateral and the rest (reward credits), so a's cash + the interval's
    flows + these = b's cash exactly."""
    items = []
    lo, hi = a["ts"], b["ts"]
    dated = [(ts, v) for ts, v in (transfers or []) if lo < ts <= hi]
    if None not in (a.get("dep"), a.get("wd"), b.get("dep"), b.get("wd")):
        d_tot = (_f(b["dep"]) - _f(b["wd"])) - (_f(a["dep"]) - _f(a["wd"]))
        if abs(sum(v for _t, v in dated) - d_tot) < 0.01:
            items += [(ts, "transfer", v) for ts, v in dated]
        elif abs(d_tot) >= 0.005:
            # a deposit dated outside the interval (an ACH that cleared late
            # keeps its start date; a returned one leaves the totals)
            items.append((hi, "transfer", d_tot))
    else:
        items += [(ts, "transfer", v) for ts, v in dated]
    items += [(ts, "transfer", v) for ts, v in (moves or []) if lo < ts <= hi]
    if a.get("R") is not None and b.get("R") is not None:
        d_r = _f(b["R"]) - _f(a["R"])
        if abs(d_r) >= 0.005:
            items.append((hi, "collateral", d_r))
    rest = (b["cash"] - a["cash"]) - flow_sum - sum(v for _t, _k, v in items)
    if abs(rest) >= 0.005:
        items.append((hi, "reward", rest))
    return items


# ----------------------------------------------------------------------------
# Where the cash is tied up
# ----------------------------------------------------------------------------

TIED_EVENTS = 80                    # rows shipped; the page shows the top ones
TIED_MARKETS = 150


def exit_value(pos: float, bid, ask) -> float:
    """What selling `pos` (> 0 YES, < 0 NO) at today's touch pays: YES at the
    YES bid, NO at the NO bid (1 - the YES ask); an empty side pays 0. Top of
    book only: the size behind it is not read."""
    if pos > 0:
        return pos * _f(bid) if _f(bid) > 0 else 0.0
    a = _f(ask)
    return -pos * (1.0 - a) if 0 < a < 1 else 0.0


def tied(positions: dict, exposure: dict, src_of, titles: dict, at: float) -> dict:
    """D.cash.tied (Jack 2026-11-14: "include the top events/markets that ties
    up cash (in case i need to emergency unwind it)"): the account's open
    positions as of one account read.

    positions: ticker -> {"ev", "pos", "cost", "bid", "ask", "mark", "shard"}
    (build_portfolio's open_positions); exposure: event -> Kalshi's event
    exposure, the cash the event ties up (its markets' cost less the
    collateral Kalshi hands back on mutually exclusive and nested ones);
    src_of(ticker) -> (row name, "imm" | "bot").

    An event's "freed" is what unwinding it at the touch would bring in:
    every market sold at the bid, less the collateral Kalshi takes back as
    the positions go. Rows: "ev" [event, title, source, group, shard,
    markets, contracts, tied, freed, value at the mark, paid (the markets'
    cost before Kalshi's collateral return)], top TIED_EVENTS by
    tied; "mk" [ticker, event, source, group, contracts (+ YES / - NO), avg
    price, cost, bid, ask, freed at the touch, shard], top TIED_MARKETS by
    cost."""
    evs, mks = {}, []
    for tk, p in (positions or {}).items():
        q = _f(p.get("pos"))
        if abs(q) < 1e-9:
            continue
        cost = _f(p.get("cost"))
        ex = exit_value(q, p.get("bid"), p.get("ask"))
        mk = p.get("mark")
        val = cost if mk is None else (q * _f(mk) if q > 0 else -q * (1.0 - _f(mk)))
        name, grp = src_of(tk)
        ev = p.get("ev") or tk
        e = evs.setdefault(ev, {"cost": 0.0, "exit": 0.0, "val": 0.0, "n": 0, "cts": 0.0,
                                "src": {}, "shard": p.get("shard")})
        e["cost"] += cost
        e["exit"] += ex
        e["val"] += val
        e["n"] += 1
        e["cts"] += abs(q)
        e["src"][(name, grp)] = e["src"].get((name, grp), 0.0) + cost
        mks.append([tk, ev, name, grp, round(q, 2), round(cost / abs(q), 4), round(cost, 2),
                    p.get("bid"), p.get("ask"), round(ex, 2), p.get("shard")])
    rows = []
    for ev, e in evs.items():
        x = exposure.get(ev) if exposure else None
        tie = _f(x) if x is not None else e["cost"]
        refund = e["cost"] - tie              # collateral handed back, taken back on exit
        (name, grp) = max(e["src"].items(), key=lambda kv: kv[1])[0]
        rows.append([ev, (titles or {}).get(ev) or "", name, grp, e["shard"], e["n"],
                     round(e["cts"], 2), round(tie, 2), round(e["exit"] - refund, 2),
                     round(e["val"] - refund, 2), round(e["cost"], 2)])
    rows.sort(key=lambda r: -r[7])
    mks.sort(key=lambda r: -r[6])
    return {"at": round(at), "tied": round(sum(r[7] for r in rows), 2),
            "freed": round(sum(r[8] for r in rows), 2),
            "cost": round(sum(e["cost"] for e in evs.values()), 2),
            "n_ev": len(rows), "n_mk": len(mks),
            "ev": rows[:TIED_EVENTS], "mk": mks[:TIED_MARKETS]}


# ----------------------------------------------------------------------------
# The payload
# ----------------------------------------------------------------------------

def build(segs: list, live: dict, samples: list, transfers, moves, src_of,
          start: float, now: float) -> dict:
    """D.cash. segs: completed days (oldest first); live: {"a0": reading,
    "flows": [...], "until": ts} since the newest snapshot, or None; samples:
    live readings; src_of(ticker, kind, fill_id) -> the source row.

    {"b": bucket seconds, "k": KINDS, "src": [name], "grp": ["imm" | "bot" |
    "acct"], "cells": [[bucket, src, kind, $]], "path": [[ts, cash | None]]
    (cash at each bucket end and at every reading; None = no data),
    "reads": [[ts, cash, "7am" | "live"]], "shards": {shard: cash} at the
    newest reading, "live_from": the first live reading, "days": [[d0, fills,
    settlements, mismatches]]}"""
    spans = []
    for s in segs:
        spans.append({"a0": s["a0"], "a1": s["a1"], "flows": s["flows"], "until": s["a1"]["ts"]})
    if live and live.get("a0"):
        last = spans[-1]["a1"]["ts"] if spans else None
        if last is None or live["a0"]["ts"] >= last - 1:
            spans.append({"a0": live["a0"], "a1": None, "flows": live.get("flows") or [],
                          "until": live["until"]})
    src_ix, srcs, grps = {}, [], []

    def sx(name, grp):
        i = src_ix.get(name)
        if i is None:
            i = src_ix[name] = len(srcs)
            srcs.append(name)
            grps.append(grp)
        return i

    cells, path, reads = {}, [], []
    kix = {k: i for i, k in enumerate(KINDS)}
    prev_end = None
    for sp in spans:
        a0, until = sp["a0"], sp["until"]
        if until < start:
            continue
        mids = [r for r in samples if a0["ts"] < r["ts"] < (sp["a1"]["ts"] if sp["a1"] else until + 1)]
        anchors = [a0] + mids + ([sp["a1"]] if sp["a1"] else [])
        flows = sorted(sp["flows"], key=lambda x: x[0])
        events = []                     # (ts, src index, kind, $)
        for ts, tk, kind, v, fid in flows:
            name, grp = src_of(tk, kind, fid)
            events.append((ts, sx(name, grp), kind, v))
        fi = 0
        for a, b in zip(anchors, anchors[1:]):
            fsum = 0.0
            while fi < len(flows) and flows[fi][0] <= b["ts"]:
                if flows[fi][0] > a["ts"]:
                    fsum += flows[fi][3]
                fi += 1
            for ts, kind, v in gap_items(a, b, fsum, transfers, moves):
                events.append((ts, sx(ACCOUNT_SRC[kind], "acct"), kind, v))
        events.sort(key=lambda e: e[0])
        if prev_end is not None and a0["ts"] - prev_end > 1:
            path.append([round((prev_end + a0["ts"]) / 2), None])      # a day not replayed
        cash, ei = a0["cash"], 0
        bnd = (int(a0["ts"] // BUCKET) + 1) * BUCKET
        marks = sorted({r["ts"] for r in anchors})
        mi = 0
        if a0["ts"] >= start:
            path.append([round(a0["ts"]), round(cash, 2)])
        end = until if sp["a1"] is None else sp["a1"]["ts"]
        while True:
            t_next = min(bnd, marks[mi] if mi < len(marks) else float("inf"), end)
            while ei < len(events) and events[ei][0] <= t_next:
                ts, si, kind, v = events[ei]
                cash += v
                if ts >= start:
                    key = (int(ts // BUCKET), si, kix[kind])
                    cells[key] = cells.get(key, 0.0) + v
                ei += 1
            if t_next >= start and t_next > a0["ts"]:
                path.append([round(t_next), round(cash, 2)])
            if t_next >= end:
                break
            if mi < len(marks) and t_next >= marks[mi]:
                mi += 1
            if t_next >= bnd:
                bnd += BUCKET
        prev_end = end
        for r in anchors:
            if r["ts"] >= start and not (reads and reads[-1][0] >= round(r["ts"])):
                reads.append([round(r["ts"]), round(r["cash"], 2), r["src"]])
    newest = samples[-1] if samples else None
    # de-duplicate path points sharing a timestamp (a reading on a bucket end)
    dd = []
    for p in path:
        if dd and dd[-1][0] == p[0]:
            dd[-1] = p
        else:
            dd.append(p)
    return {"b": BUCKET, "k": list(KINDS), "src": srcs, "grp": grps,
            "cells": [[b, s, k, round(v, 2)] for (b, s, k), v in sorted(cells.items())
                      if abs(v) >= 0.005],
            "path": dd, "reads": reads,
            "shards": (newest or {}).get("sh"),
            "shards_at": round(newest["ts"]) if newest else None,
            "live_from": round(samples[0]["ts"]) if samples else None,
            "days": [[s["d0"], s.get("fills"), s.get("setts"), s.get("mism")] for s in segs],
            "until": round(spans[-1]["until"]) if spans else None}
