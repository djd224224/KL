#!/usr/bin/env python3
r"""imm_astest_picks.py -- the opportunistic AS test's DAILY PICKS (Jack
2026-10-27: "Turn back on opportunistic bot but now pick 5 distinct events
each day, optimizing for likelihood of profitability, even better if fair
value can be built, quote it at 1x size, and add it to the opportunistic
daily report email").

Once a day this ranks every paying event the bot does not otherwise quote and
writes run-logs\incentive-mm\astest_picks.json, which the live bot reads at
each universe refresh (incentive_mm.load_astest_picks: the picked events are
quoted x1 under the AS test's stops). Read-only on the exchange.

THE UNIVERSE: each market of an unpaid liquidity program (the bot's own
fetch_programs) that the normal book does not quote -- not _allowed, not
another AS family (Billboard, VoteHub) -- and is not blocked, with one
exception: the AAA gas state dailies, the AS test's first family, whose
state-pattern block a pick lifts. Then the open scan's screens, which fail
closed: no other bot's family or live-index price structure
(SCAN_EXCLUDE_PREFIXES), no live settlement feed where the reward window
covers the resolution (scan_series_meta_verdict / live_reward_overlaps), a
cutoff the bot can derive -- a day in the ticker (the midnight-ET rule) or
Kalshi's published occurrence -- with numeric-threshold strikes
(scan_shape_reason: a "will X happen" binary's one jump is its resolution),
no Fiscal.ai company KPI (the type blocked 10/04, "unless there is a fair
value"), an event that lives at least MIN_EVENT_LIFE_H (no hourly or
15-minute shapes), no insider-decided title (imm_family_watch's
_ONE_OFF_INSIDER_RE), and no event of Jack's own positions (manual_events:
the bot yields to him there anyway).

THE SCORE -- "likelihood of profitability" -- for each event, over the day a
pick lasts (HORIZON_H, intersected with its cutoff, program end, close and
quote window):
  reward  the bot's own estimator at the pick's x1 sizing
          (_estimate_candidate_yield on the live book), market by market,
          counting a market only where its projection clears the bot's payout
          floor (the bot would not admit it otherwise) and the day's share
          clears Kalshi's $1 minimum; a window that opens later (the gas
          states' 16:00-24:00 ET) is scored at WINDOW_HAIRCUT of the current
          book's estimate -- on 10/26 the gas evening earned ~6% of the
          10:11 ET estimate as the books converged out of the band
  fills   per past event j of the series, the contracts that traded in the
          same hours before the cutoff (the public tape of the last
          HIST_EVENTS settled events), times our share of the touch,
          q = rung / (rung + resting size at the touch), capped at
          FILL_CAP_PER_H (the 15-minute event pause after each fill)
  cost    each filled contract at AS_MULT times what the average maker lost a
          contract on event j, never under AS_FLOOR_C -- our fills are picked
          off worse than the tape's average maker (the gas window of 10/26:
          makers -3.2c a contract, ours -31c)
  net_j   reward - fills_j x cost_j; P(profit) = the share of past events
          with net_j > 0; also the mean and the 25th percentile
An event needs MIN_HIST_EVENTS settled past events (no history, no pick),
P(profit) >= MIN_P_PROFIT and a mean net >= MIN_NET. The ranking is
P(profit) first, then a FAIR VALUE ("even better if fair value can be
built"): "buildable" when the series settles on a public statistic with a
history a model can be fitted to (FAIR_SOURCE_KEYWORDS: AAA, EIA, BLS, ...),
"none" otherwise -- then the mean net. The picks are DISTINCT: one event per
series, and one per shared driver (driver_group: the 51 gas states move
together on the national price), at most PICK_COUNT. An event picked
yesterday keeps its seat on a tie (incumbents first).

usage:
  python imm_astest_picks.py             rank, write astest_picks.json, append
                                         astest_picks_history.jsonl
  python imm_astest_picks.py --dry-run   rank and print only (writes only the
                                         tape cache, astest_picks_cache.json)
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import imm_quote_gaps as gaps          # noqa: E402  (mirrors the launcher env first)
import incentive_mm as imm             # noqa: E402

ET = ZoneInfo("America/New_York")
PICKS_PATH = os.path.join(imm.STATUS_DIR, imm.ASTEST_PICK_FILE)
HISTORY_PATH = os.path.join(imm.STATUS_DIR, "astest_picks_history.jsonl")
CACHE_PATH = os.path.join(imm.STATUS_DIR, "astest_picks_cache.json")
STATE_PATH = os.path.join(imm.STATUS_DIR, "imm_state.json")

PICK_COUNT = 5
HORIZON_H = 24.0
MIN_EVENT_LIFE_H = 6.0
MIN_HIST_EVENTS = 3
HIST_EVENTS = 5
HIST_DAYS = 150
HIST_MARKETS_PER_EVENT = 8
HIST_MAX_HB = 24 * 14              # trades further out pool in one bucket
MIN_P_PROFIT = 0.6
MIN_NET = 0.50
AS_MULT = 3.0
AS_FLOOR_C = 1.0
FILL_CAP_PER_H = 80.0
WINDOW_HAIRCUT = 0.10
GAS_WINDOW_ET = "16:00-24:00"
MAX_BOOKS = 500
MAX_BOOKS_PER_EVENT = 12
TAPE_PER_GROUP = 2
TAPE_GROUPS_MAX = 25
SERIES_TTL_S = 7 * 86400.0
PUBLIC_API = "https://api.elections.kalshi.com/trade-api/v2"
TAPE_PAUSE_S = 0.5
FAIR_SOURCE_KEYWORDS = (
    "aaa.com", "eia.gov", "bls.gov", "census.gov", "bea.gov", "treasury.gov",
    "federalreserve.gov", "stlouisfed.org", "usda.gov", "tsa.gov", "cdc.gov",
    "dol.gov", "the-numbers.com", "boxofficemojo.com", "noaa.gov",
    "freddiemac.com", "zillow.com", "redfin.com", "nar.realtor")
_GAS_STATE_RE = re.compile(r"KXAAAGASD([A-Z][A-Z])")
# the fuel regions the 10/26 gas test took one state from (Jack 2026-10-26:
# "Run a smaller test first, not 51 markets that are correlated")
GAS_REGION = {st: reg for reg, sts in (
    ("New England", "CT MA ME NH RI VT"), ("Central Atlantic", "DC DE MD NJ NY PA"),
    ("Lower Atlantic", "FL GA NC SC VA WV"),
    ("Midwest", "IA IL IN KS KY MI MN MO ND NE OH OK SD TN WI"),
    ("Gulf Coast", "AL AR LA MS NM TX"), ("Rockies", "CO ID MT UT WY"),
    ("West Coast", "AK AZ CA HI NV OR WA")) for st in sts.split()}
# a Fiscal.ai-settled company KPI: the type blocked 10/04 (36 series lost on
# trading every week; de59604) -- "leave it unless there is a fair value"
KPI_SOURCE_KEYWORDS = ("fiscal.ai",)

_N_PUBLIC = [0]
VERBOSE = [False]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z {msg}", flush=True)


def vlog(msg: str) -> None:
    if VERBOSE[0]:
        log("  . " + msg)


# ---- pure pieces (tested) ---------------------------------------------------

def driver_group(series: str) -> str:
    """The shared driver a pick must not double up on: a gas state's fuel
    region (the states move together on the national price; the 10/26 test
    took one a region); anything else is its own series."""
    m = _GAS_STATE_RE.fullmatch(series)
    if m:
        return f"AAA gas: {GAS_REGION.get(m.group(1), m.group(1))}"
    return series


def quote_hours(start: datetime, end: datetime,
                window: Optional[Tuple[str, str]], step_min: int = 15) -> float:
    """Hours in [start, end) inside the daily ET window [a, b) (b may be
    24:00, and b < a wraps past midnight); every hour with no window."""
    if end <= start:
        return 0.0
    if window is None:
        return (end - start).total_seconds() / 3600.0
    a = int(window[0][:2]) * 60 + int(window[0][3:])
    b = int(window[1][:2]) * 60 + int(window[1][3:])
    n, t = 0, start
    while t < end:
        lt = t.astimezone(ET)
        m = lt.hour * 60 + lt.minute
        if (a <= m < b) if a < b else (m >= a or m < b):
            n += 1
        t += timedelta(minutes=step_min)
    return n * step_min / 60.0


def quoted_hbs(now: datetime, end: datetime, cutoff: datetime,
               window: Optional[Tuple[str, str]]) -> List[int]:
    """The hours-before-cutoff buckets (0 = the last hour before the cutoff)
    the pick quotes in, from now to `end`, inside its window."""
    out = []
    t = now.replace(minute=0, second=0, microsecond=0)
    while t < end:
        mid = t + timedelta(minutes=30)
        if mid >= now and quote_hours(t, t + timedelta(hours=1), window) > 0:
            hb = int((cutoff - mid).total_seconds() // 3600)
            if hb >= 0:
                out.append(min(hb, HIST_MAX_HB))
        t += timedelta(hours=1)
    return out


def touch_share(rung: float, depth: Optional[float]) -> float:
    """Our share of what trades at the touch we join: pro rata against the
    size already resting there (we queue behind it). Unknown depth = 0.5."""
    if depth is None:
        return 0.5
    return rung / (rung + max(0.0, depth))


def score_event(reward: float, q: float, quote_h: float,
                hist: List[Tuple[float, float]]) -> Dict[str, float]:
    """`hist`: per past event (contracts an hour in the pick's hours, makers'
    P&L in cents a contract). Our fills price at AS_MULT x the makers' loss,
    never under AS_FLOOR_C."""
    nets, fills_l, cost_l = [], [], []
    for v, maker_c in hist:
        fills = min(q * v, FILL_CAP_PER_H) * quote_h
        cost = max(AS_FLOOR_C, AS_MULT * max(0.0, -maker_c))
        nets.append(reward - fills * cost / 100.0)
        fills_l.append(fills)
        cost_l.append(cost)
    if not nets:
        return {"n": 0, "p_profit": 0.0, "net_mean": 0.0, "net_p25": 0.0,
                "fills": 0.0, "cost_c": 0.0}
    srt = sorted(nets)
    return {"n": len(nets),
            "p_profit": sum(1 for x in nets if x > 0) / len(nets),
            "net_mean": sum(nets) / len(nets),
            "net_p25": srt[int(0.25 * (len(srt) - 1))],
            "fills": sum(fills_l) / len(fills_l),
            "cost_c": sum(cost_l) / len(cost_l)}


def eligible(row: dict) -> bool:
    s = row.get("score") or {}
    return (s.get("n", 0) >= MIN_HIST_EVENTS and s.get("p_profit", 0.0) >= MIN_P_PROFIT
            and s.get("net_mean", 0.0) >= MIN_NET)


def rank_key(row: dict, incumbents=frozenset()):
    s = row["score"]
    return (-round(s["p_profit"], 2), -(1 if row.get("fair") == "buildable" else 0),
            -(1 if row["event"] in incumbents else 0), -round(s["net_mean"], 2))


def choose(rows: List[dict], incumbents=frozenset(), n: int = PICK_COUNT) -> List[dict]:
    """The best `n` eligible rows, one per series and one per driver group.
    Yesterday's picks that are still eligible keep their seats first: a
    multi-day event's reward pays only once its period total clears $1, so
    a pick dropped after a day can forfeit what it accrued."""
    out, groups, series = [], set(), set()
    ranked = sorted((r for r in rows if eligible(r)), key=lambda r: rank_key(r, incumbents))
    for r in ([r for r in ranked if r["event"] in incumbents]
              + [r for r in ranked if r["event"] not in incumbents]):
        g = driver_group(r["series"])
        if r["series"] in series or g in groups:
            continue
        out.append(r)
        groups.add(g)
        series.add(r["series"])
        if len(out) >= n:
            break
    return out


def kpi_series(sources: List[str]) -> bool:
    return any(k in str(b).lower() for b in sources or [] for k in KPI_SOURCE_KEYWORDS)


def fair_status(sources: List[str]) -> Tuple[str, str]:
    """("buildable", the matching source) when the settlement source is a
    public statistic with a history, else ("none", "")."""
    for blob in sources or []:
        low = str(blob).lower()
        for k in FAIR_SOURCE_KEYWORDS:
            if k in low:
                return "buildable", k
    return "none", ""


def tape_aggregate(trades: List[dict], cutoff: datetime,
                   mark: float) -> Dict[str, List[float]]:
    """Trades -> {hours-before-cutoff bucket: [contracts, makers' P&L $]}, the
    maker being the side opposite the taker, marked to `mark` (cents); trades
    at or after the cutoff are left out (the bot is out by then)."""
    out: Dict[str, List[float]] = {}
    for tr in trades:
        ts = imm.parse_iso_utc(tr.get("created_time") or "")
        if ts is None or ts >= cutoff:
            continue
        try:
            yp = float(tr.get("yes_price_dollars") or 0) * 100.0
            n = float(tr.get("count_fp") or tr.get("count") or 0)
        except (TypeError, ValueError):
            continue
        taker = n * ((mark - yp) if tr.get("taker_side") == "yes" else (yp - mark)) / 100.0
        hb = str(min(int((cutoff - ts).total_seconds() // 3600), HIST_MAX_HB))
        c = out.setdefault(hb, [0.0, 0.0])
        c[0] += n
        c[1] -= taker
    return out


def event_rate(markets: List[dict], hbs: List[int], life_h: float
               ) -> Optional[Tuple[float, float]]:
    """A past event's (contracts an hour, makers' cents a contract) over the
    pick's hour buckets, from its markets' aggregates. The maker figure falls
    back to the event's whole pre-cutoff tape when the buckets carry under 50
    contracts. None when the event did not live through any of the buckets."""
    live = [hb for hb in hbs if hb < max(1.0, life_h)]
    if not live:
        return None
    ct = pnl = ct_all = pnl_all = 0.0
    want = {str(h) for h in live}
    for m in markets:
        for hb, (c, p) in (m.get("hb") or {}).items():
            ct_all += c
            pnl_all += p
            if hb in want:
                ct += c
                pnl += p
    if ct >= 50:
        maker_c = 100.0 * pnl / ct
    elif ct_all > 0:
        maker_c = 100.0 * pnl_all / ct_all
    else:
        maker_c = 0.0
    return ct / len(live), maker_c


# ---- I/O ----------------------------------------------------------------------

def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # a reader holds it (Windows); retry
            time.sleep(0.5)
    os.replace(tmp, path)


def public_get(path: str, **q) -> dict:
    """The public market-data API at ~2 requests/s, so the live bot keeps its
    own rate headroom. 404 -> {}."""
    url = PUBLIC_API + path + ("?" + urllib.parse.urlencode(q) if q else "")
    for a in range(8):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                out = json.load(r)
            _N_PUBLIC[0] += 1
            time.sleep(TAPE_PAUSE_S)
            return out
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5 * (a + 1))
                continue
            if e.code == 404:
                return {}
            raise
        except (urllib.error.URLError, TimeoutError, ValueError):
            time.sleep(3)
    raise RuntimeError(f"public read failed: {path}")


def settled_markets(series: str, since_ts: int) -> List[dict]:
    out, cur = [], None
    for _ in range(15):
        q = {"series_ticker": series, "status": "settled", "limit": 200,
             "min_close_ts": since_ts}
        if cur:
            q["cursor"] = cur
        js = public_get("/markets", **q)
        out += js.get("markets") or []
        cur = js.get("cursor")
        if not cur or not js.get("markets"):
            break
    return out


def market_trades(ticker: str, min_ts: int) -> List[dict]:
    out, cur = [], None
    for _ in range(40):
        q = {"ticker": ticker, "limit": 1000, "min_ts": min_ts}
        if cur:
            q["cursor"] = cur
        js = public_get("/markets/trades", **q)
        out += js.get("trades") or []
        cur = js.get("cursor")
        if not cur or not js.get("trades"):
            break
    return out


def base_cutoff(event_ticker: str, m: dict) -> Optional[datetime]:
    """The bot's base cutoff rule (trade_cutoff_utc: 00:00 ET of the ticker
    date, or Kalshi's occurrence) on a market object's own fields."""
    return imm.trade_cutoff_utc(
        event_ticker, imm.parse_iso_utc(m.get("occurrence_datetime") or ""),
        imm.parse_iso_utc(m.get("expected_expiration_time") or ""))


def past_cutoff(event_ticker: str, markets: List[dict],
                offset: timedelta) -> Optional[datetime]:
    """A settled event's cutoff under the bot's rule, less the same offset
    the live event's cutoff sits from its own base (a series adjustment)."""
    for m in markets:
        c = base_cutoff(event_ticker, m)
        if c is not None:
            return c - offset
    return None


def series_history(series: str, offset: timedelta, cache: dict,
                   now: datetime) -> List[dict]:
    """The series' last HIST_EVENTS settled events: [{event, life_h,
    markets: [{ticker, hb}]}], each market's tape aggregated once and kept
    in the cache (a settled market's tape never changes)."""
    mk = settled_markets(series, int((now - timedelta(days=HIST_DAYS)).timestamp()))
    by_ev: Dict[str, List[dict]] = {}
    for m in mk:
        if m.get("result") in ("yes", "no"):
            by_ev.setdefault(m.get("event_ticker") or "", []).append(m)
    evs = sorted((e for e in by_ev if e),
                 key=lambda e: max(m.get("close_time") or "" for m in by_ev[e]),
                 reverse=True)[:HIST_EVENTS]
    mcache = cache.setdefault("markets", {})
    out = []
    for ev in evs:
        cut = past_cutoff(ev, by_ev[ev], offset)
        if cut is None:
            continue
        ms = sorted(by_ev[ev], key=lambda m: -float(m.get("volume_fp") or m.get("volume") or 0))
        ms = ms[:HIST_MARKETS_PER_EVENT]
        opens = [imm.parse_iso_utc(m.get("open_time") or "") for m in ms]
        opens = [o for o in opens if o is not None]
        life_h = (cut - min(opens)).total_seconds() / 3600.0 if opens else HORIZON_H
        rows = []
        for m in ms:
            t = m["ticker"]
            ent = mcache.get(t)
            if ent is None or ent.get("cut") != cut.isoformat():
                if float(m.get("volume_fp") or m.get("volume") or 0) <= 0:
                    ent = {"cut": cut.isoformat(), "hb": {}}
                else:
                    mark = 100.0 if m.get("result") == "yes" else 0.0
                    since = int((cut - timedelta(hours=HIST_MAX_HB + 24)).timestamp())
                    ent = {"cut": cut.isoformat(),
                           "hb": tape_aggregate(market_trades(t, since), cut, mark)}
                mcache[t] = ent
            rows.append({"ticker": t, "hb": ent["hb"]})
        out.append({"event": ev, "life_h": life_h, "markets": rows})
    return out


# ---- the run -------------------------------------------------------------------

def candidate_reason(t: str, info: dict, quoted: set, incumbents: set,
                     now: datetime) -> Optional[str]:
    """None for a market the picks may consider, else why not (string-level
    screens only; the market and series reads come after)."""
    series = imm.series_of(t)
    ev = t.rsplit("-", 1)[0]
    if info.get("dollars_per_day", 0) <= 0:
        return "no pool"
    if t in quoted and ev not in incumbents:
        return "quoted"
    if gaps.ticker_day_over(t, now):
        return "day over"
    if imm.astest_member_family(series) in ("BB", "VH"):
        return "AS family"
    gas = _GAS_STATE_RE.fullmatch(series) is not None
    if any(t.startswith(p) for p in imm.SERIES_BLOCKLIST_PREFIXES) \
            or series in imm.FREEZE_SERIES:
        return "blocked"
    if not gas:
        if imm.IncentiveMarketMaker._blocked(t):
            return "blocked"
        if imm.IncentiveMarketMaker._allowed(t):
            return "normal book"
    if any(series.startswith(p) for p in imm.SCAN_EXCLUDE_PREFIXES):
        return "excluded family"
    return None


def insider_title(title: str) -> Optional[str]:
    try:
        import imm_family_watch as fw    # noqa: E402
        m = fw._ONE_OFF_INSIDER_RE.search(title or "")
        return m.group(0) if m else None
    except Exception:                      # noqa: BLE001
        return None


def run(dry_run: bool = False, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    imm.load_file_event_overrides()
    imm.load_extra_allow_series()
    cache = load_json(CACHE_PATH, {})
    prev = load_json(PICKS_PATH, {})
    incumbents = {str(p.get("event")) for p in (prev.get("picks") or [])}
    state = load_json(STATE_PATH, {})
    quoted = set(state.get("selected_tickers") or [])

    client = imm.build_client()
    bot = imm.IncentiveMarketMaker(client, live=False)
    programs = bot.fetch_programs() or {}
    if not programs:
        raise RuntimeError("fetch_programs returned nothing")
    try:
        manual = bot.manual_events(bot.fetch_positions())
    except Exception as e:                 # noqa: BLE001
        log(f"! positions read failed ({e!r}); manual events not screened")
        manual = set()

    why_n: Dict[str, int] = {}
    cand: Dict[str, dict] = {}
    for t, info in programs.items():
        why = candidate_reason(t, info, quoted, incumbents, now)
        if why is None and t.rsplit("-", 1)[0] in manual:
            why = "manual"
        if why:
            why_n[why] = why_n.get(why, 0) + 1
            continue
        cand[t] = info
    log(f"programs {len(programs)}; candidates {len(cand)} markets; out: "
        + ", ".join(f"{k} {v}" for k, v in sorted(why_n.items(), key=lambda kv: -kv[1])))

    details = gaps.bulk_market_details(client, sorted(cand))
    # score every candidate as if picked: x1 at every hour, the pick's caps,
    # the gas states' quote window (the overlay the bot applies)
    series_set = {imm.series_of(t) for t in cand}
    imm.ASTEST_PICK_SERIES = frozenset(series_set)
    imm.astest_stopped = lambda: False          # this process only
    for s in sorted(series_set):
        imm._astest_pick_overlay(
            s, imm._astest_pick_window(GAS_WINDOW_ET) if _GAS_STATE_RE.fullmatch(s) else None)

    sources: Dict[str, List[str]] = {}
    series_ok: Dict[str, Tuple[bool, str]] = {}
    scache = cache.setdefault("series", {})
    by_event: Dict[str, List[Tuple[str, dict, dict]]] = {}
    shape_n: Dict[str, int] = {}
    for t, info in cand.items():
        m = details.get(t)
        if not m or m.get("status") not in ("active", "open"):
            shape_n["not open"] = shape_n.get("not open", 0) + 1
            continue
        meta = gaps.build_meta(bot, t, info, m, now)
        # the open scan's STRUCTURE screen: a day in the ticker or a cutoff
        # the bot can derive (Kalshi's occurrence), and a numeric strike
        sh = imm.scan_shape_reason(t, strike_type=m.get("strike_type"),
                                   cutoff_known=meta.cutoff is not None)
        if sh is None and meta.cutoff is None:
            sh = "no cutoff"
        if sh:
            shape_n[sh] = shape_n.get(sh, 0) + 1
            continue
        ev = m.get("event_ticker") or t.rsplit("-", 1)[0]
        by_event.setdefault(ev, []).append((t, info, m, meta))
    log(f"events {len(by_event)} after the shape screen (out: "
        + ", ".join(f"{k} {v}" for k, v in shape_n.items()) + ")")

    # the series screen (cached GET /series), then the per-event screens
    rows = []
    for ev, mlist in by_event.items():
        series = imm.series_of(ev)
        if series not in series_ok:
            ent = scache.get(series)
            if not ent or now.timestamp() - float(ent.get("ts", 0)) > SERIES_TTL_S:
                try:
                    so = (client.get(f"/series/{series}") or {}).get("series") or {}
                except Exception as e:     # noqa: BLE001
                    log(f"! series read failed {series}: {e!r}")
                    so = {}
                ok, why, cat = imm.scan_series_meta_verdict(so)
                ent = {"ts": now.timestamp(), "ok": ok, "why": why, "category": cat,
                       "title": so.get("title") or "",
                       "sources": imm.series_source_blobs(so) if so else []}
                if why != "series_meta_unavailable":
                    scache[series] = ent
            series_ok[series] = (bool(ent.get("ok")), str(ent.get("why") or ""))
            sources[series] = list(ent.get("sources") or [])
        ok, why = series_ok[series]
        if kpi_series(sources[series]):
            vlog(f"{ev}: company KPI (Fiscal.ai)")
            continue
        metas = []
        for t, info, m, meta in mlist:
            if not ok and not (why == "live_source" and not imm.live_reward_overlaps(meta)):
                metas = []
                break
            metas.append((t, info, m, meta))
        if not metas:
            vlog(f"{ev}: series screen {why}")
            continue
        opens = [x[3].open_time for x in metas if x[3].open_time]
        closes = [x[3].close_time for x in metas if x[3].close_time]
        if opens and closes and (max(closes) - min(opens)).total_seconds() < MIN_EVENT_LIFE_H * 3600:
            vlog(f"{ev}: lives under {MIN_EVENT_LIFE_H:g}h")
            continue
        title = str(metas[0][2].get("title") or "")
        word = insider_title(title)
        if word:
            vlog(f"{ev}: insider word '{word}' in '{title[:60]}'")
            continue
        rows.append({"event": ev, "series": series, "metas": metas,
                     "pool": sum(x[1]["dollars_per_day"] for x in metas),
                     "title": title})
    log(f"events {len(rows)} after the series and event screens")

    # ---- reward: the bot's estimator on the live book, x1 -------------------
    books: Dict[str, dict] = {}
    _orig = bot._read_candidate_book

    def _capture(t):
        ob = _orig(t)
        books[t] = ob
        return ob
    bot._read_candidate_book = _capture
    rung = float(sum(s for _o, s in imm.LEVELS[:1])) or 20.0
    n_books = 0
    rows.sort(key=lambda r: -r["pool"])
    scored = []
    for r in rows:
        if n_books >= MAX_BOOKS:
            break
        window = imm._astest_pick_window(GAS_WINDOW_ET) if _GAS_STATE_RE.fullmatch(r["series"]) else None
        reward, depths, n_mk, ends, cutoffs = 0.0, [], 0, [], []
        inband, why_m = [], {}
        for t, info, m, meta in r["metas"]:
            sr = bot._screen(meta, now)
            if sr:
                why_m[sr] = why_m.get(sr, 0) + 1
                continue
            inband.append((t, info, m, meta))
        for t, info, m, meta in sorted(inband, key=lambda x: -x[1]["dollars_per_day"])[:MAX_BOOKS_PER_EVENT]:
            n_books += 1
            try:
                if not bot._estimate_candidate_yield(meta, []):
                    why_m["book unreadable"] = why_m.get("book unreadable", 0) + 1
                    continue
            except Exception as e:         # noqa: BLE001
                log(f"! estimate failed {t}: {e!r}")
                continue
            est = float(meta.est_dollars_per_day or 0.0)
            if est <= 0:
                why_m["zero yield"] = why_m.get("zero yield", 0) + 1
                continue
            caps = [x for x in (meta.cutoff, meta.program_end, meta.close_time) if x is not None]
            end = min([now + timedelta(hours=HORIZON_H)] + caps)
            h = quote_hours(now, end, window)
            if h <= 0:
                why_m["no hours"] = why_m.get("no hours", 0) + 1
                continue
            day = est * h / 24.0
            if window is not None and quote_hours(now, now + timedelta(minutes=15), window) == 0:
                day *= WINDOW_HAIRCUT
            # the bot's own entry bar on the projection over the market's
            # whole quotable window (it would not admit the market otherwise)
            proj = meta.floor_dollars_per_day * imm._quotable_days(meta, now)
            if proj < imm.floor_bar_dollars(meta.series, False):
                why_m["payout floor"] = why_m.get("payout floor", 0) + 1
                continue
            reward += day
            n_mk += 1
            ends.append(end)
            if meta.cutoff is not None:
                cutoffs.append(meta.cutoff)
                if "offset" not in r:
                    b = base_cutoff(r["event"], m)
                    r["offset"] = (b - meta.cutoff) if b is not None else timedelta(0)
            ob = books.get(t)
            if ob:
                yl, nl = imm.orderbook_levels(ob)
                if yl:
                    depths.append(max(yl, key=lambda x: x[0])[1])
                if nl:
                    depths.append(max(nl, key=lambda x: x[0])[1])
        if n_mk == 0:
            vlog(f"{r['event']}: no market rewarded ({', '.join(f'{k} {v}' for k, v in why_m.items())})")
            continue
        depths.sort()
        q = touch_share(rung, depths[len(depths) // 2] if depths else None)
        end = max(ends)
        cutoff = min(cutoffs) if cutoffs else end
        r.update({"reward": reward, "n_markets": n_mk, "q": q, "end": end,
                  "cutoff": cutoff, "window": window,
                  "quote_h": quote_hours(now, end, window)})
        vlog(f"{r['event']}: reward ${reward:.2f} on {n_mk} markets, q {q:.2f} "
             f"({', '.join(f'{k} {v}' for k, v in why_m.items()) or 'all in'})")
        scored.append(r)
    log(f"rewarded {len(scored)} events on {n_books} book reads")

    # ---- the tape: per driver group, the best TAPE_PER_GROUP by reward ----
    scored.sort(key=lambda r: -r["reward"])
    per_group: Dict[str, int] = {}
    tape_rows = []
    for r in scored:
        g = driver_group(r["series"])
        if per_group.get(g, 0) >= TAPE_PER_GROUP:
            continue
        if len(per_group) >= TAPE_GROUPS_MAX and g not in per_group:
            continue
        per_group[g] = per_group.get(g, 0) + 1
        tape_rows.append(r)
    hist_by_series: Dict[str, List[dict]] = {}
    for r in tape_rows:
        s = r["series"]
        offset = r.get("offset") or timedelta(0)
        if s not in hist_by_series:
            t0 = time.time()
            try:
                hist_by_series[s] = series_history(s, offset, cache, now)
            except Exception as e:         # noqa: BLE001
                log(f"! tape read failed {s}: {e!r}")
                hist_by_series[s] = []
            log(f"tape {s}: {len(hist_by_series[s])} settled events, "
                f"{time.time() - t0:.0f}s, {_N_PUBLIC[0]} public reads so far")
        hbs = quoted_hbs(now, r["end"], r["cutoff"], r["window"])
        hist = []
        for pe in hist_by_series[s]:
            er = event_rate(pe["markets"], hbs, pe["life_h"])
            if er is not None:
                hist.append(er)
        r["score"] = score_event(r["reward"], r["q"], r["quote_h"], hist)
        r["fair"], r["fair_source"] = fair_status(sources.get(s) or [])
    # the tape cache is kept even on a dry run: a settled market's tape never
    # changes, and the next run reads only what settled since
    write_json_atomic(CACHE_PATH, cache)

    picks = choose(tape_rows, frozenset(incumbents))
    ranked = sorted((r for r in tape_rows if "score" in r),
                    key=lambda r: rank_key(r, frozenset(incumbents)))

    def _out(r):
        s = r["score"]
        return {"event": r["event"], "series": r["series"], "title": r["title"],
                "group": driver_group(r["series"]),
                "window_et": "-".join(r["window"]) if r["window"] else None,
                "reward": round(r["reward"], 2), "n_markets": r["n_markets"],
                "q": round(r["q"], 3), "quote_h": round(r["quote_h"], 2),
                "fills": round(s["fills"], 1), "cost_c": round(s["cost_c"], 2),
                "net_mean": round(s["net_mean"], 2), "net_p25": round(s["net_p25"], 2),
                "p_profit": round(s["p_profit"], 2), "n_hist": s["n"],
                "fair": r["fair"], "fair_source": r["fair_source"],
                "cutoff": r["cutoff"].isoformat(), "end": r["end"].isoformat(),
                "incumbent": r["event"] in incumbents, "eligible": eligible(r)}
    result = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pick_day_et": now.astimezone(ET).strftime("%Y-%m-%d"),
        "model": {"horizon_h": HORIZON_H, "as_mult": AS_MULT, "as_floor_c": AS_FLOOR_C,
                  "fill_cap_per_h": FILL_CAP_PER_H, "window_haircut": WINDOW_HAIRCUT,
                  "min_p_profit": MIN_P_PROFIT, "min_net": MIN_NET,
                  "min_hist_events": MIN_HIST_EVENTS, "hist_events": HIST_EVENTS},
        "considered": {"programs": len(programs), "candidate_markets": len(cand),
                       "events": len(rows), "rewarded": len(scored),
                       "scored": len(ranked), "eligible": sum(1 for r in ranked if eligible(r)),
                       "public_reads": _N_PUBLIC[0], "book_reads": n_books},
        "picks": [_out(r) for r in picks],
        "ranked": [_out(r) for r in ranked[:25]],
    }
    if not dry_run:
        write_json_atomic(PICKS_PATH, result)
        with open(HISTORY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"generated_at": result["generated_at"],
                                "picks": [p["event"] for p in result["picks"]]}) + "\n")
    return result


def report(result: dict) -> str:
    lines = [f"astest picks {result['generated_at']} (pick day {result['pick_day_et']}): "
             f"{len(result['picks'])} of {PICK_COUNT}; "
             + ", ".join(f"{k} {v}" for k, v in result["considered"].items())]
    for tag, rows in (("PICK", result["picks"]), ("ranked", result["ranked"])):
        for p in rows:
            lines.append(
                f"  {tag:6} {p['event']:34} P {p['p_profit']:.2f} n{p['n_hist']} "
                f"net ${p['net_mean']:+.2f} (p25 {p['net_p25']:+.2f}) reward ${p['reward']:.2f} "
                f"on {p['n_markets']} mkts {p['quote_h']:.0f}h, fills {p['fills']:.0f} @ "
                f"{p['cost_c']:.1f}c, q {p['q']:.2f}, fair {p['fair']}"
                + (" (incumbent)" if p["incumbent"] else "")
                + ("" if p["eligible"] else " [not eligible]")
                + f" -- {p['title'][:60]}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="log each event's screen")
    a = ap.parse_args(argv)
    VERBOSE[0] = a.verbose
    t0 = time.time()
    try:
        res = run(dry_run=a.dry_run)
    except Exception as e:                 # noqa: BLE001
        log(f"! astest picks failed: {e!r}")
        return 1
    print(report(res), flush=True)
    log(f"astest picks done in {time.time() - t0:.0f}s"
        + (" (dry run, nothing written)" if a.dry_run else f" -> {PICKS_PATH}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
