#!/usr/bin/env python3
"""follow_probe.py -- do other makers follow a 1-lot at the theoretical price?

Jack 2026-11-08: "build a bot that puts out 1 contract limit orders at the
best theoretical limit price on wide books (10c+) and sees if other makers
follow", then "scan incentive rewards markets only".

THE EXPERIMENT
  Universe: every market with an ACTIVE Kalshi liquidity-incentive program
  (GET /incentive_programs?status=active), on exchange shard 0, quoted on
  both sides with a spread of FPROBE_MIN_SPREAD_C (10c) or more and under
  FPROBE_MAX_SPREAD_C (40c; a 9/95 stub's mid is no theoretical price). Left out:
  events where the primary account (the IMM, the crypto bots) rests any order
  (FPROBE_IMM_EXCLUDE=market narrows that to the market itself), and the temp
  bot's series. During a trial, any book change our own API key causes
  carries a client_order_id on the WebSocket (verified live 11/08: deltas from
  the IMM's and the crypto bots' orders carry client_order_id + subaccount,
  other makers' do not). A trial in which one of OUR other bots touched the
  market is kept out of the score: the question is about OTHER makers.

  Probes. One market per event at a time, drawn from two strata ("active":
  traded in the last 24 h; "quiet": not), side (bid or ask) by a coin flip.
  NO CONTROL TWIN (Jack 2026-11-08: "dont need a twin", "if something moves
  to join, can attribute it to the probe"): our price sits inside a 10c+ gap
  where nobody rests, so a maker joining or stepping ahead of it is a
  reaction to it. The 11/08 dry run backs that: with no order, 0 of 24 books
  got anyone at the mid within 10 min. The report shows the dry runs' rate
  beside the live one as the no-order baseline.

  Theoretical price: the mid of the LIVE WebSocket book at placement, rounded
  toward our side: a bid at floor(mid), an ask at ceil(mid). That is the best
  price a maker can rest at without paying over the mid. FPROBE_EDGE_C backs
  it off by that many cents. The probe rests 1 contract there, post-only, for
  FPROBE_HOLD_S (600 s), then cancels. It never reacts to what others do: it
  rests its whole hold and can be filled. It is a real order, never pulled
  because someone followed it.

  A trial: subscribe -> pre (FPROBE_PRE_S, 60 s of baseline) -> t0: price
  and place -> hold -> t1: cancel -> post (FPROBE_POST_S, 300 s: does the
  follower go back?) -> one row in trials_<UTC day>.jsonl.

  Kalshi's reward reference is the best level holding target/5 contracts
  (see the IMM's lone-reference guard), so ONE contract does not move the
  reward reference. Makers that follow it are following the VISIBLE best
  price, not the reward score.

WHAT IS MEASURED (in "own-price space": our side's levels, higher = better;
an ask at YES price P is a NO bid at 100 - P). "Others" is the book minus
our own resting contract. An event counts once it has stood
FPROBE_DEBOUNCE_S (1 s), and it is timed from when it began:
  join      another maker rests AT our price
  improve   another maker rests BETTER than our price (pennies us)
  follow    join or improve: someone else at our price or better
  step      others' best on our side rose above where it stood at t0
  opp_step  the other side's best came in toward us
  gap_closed  how far others' best moved from its t0 level toward our price
            (1.0 = all the way, over 1 = past it)
  present_t1  a follower is still at/over our price when we cancel
  gone_s    after our cancel, seconds until that follower left (post phase)
  revert    how far others' best fell back toward its t0 level after cancel
The report gives each rate with a 90% bootstrap interval over probes.

DRY BY DEFAULT. Without --live nothing is placed: a dry run measures the
same numbers against the price we WOULD have placed -- the no-order baseline.

LIVE needs --live --subaccount N, a numbered Kalshi subaccount that holds
nothing else (Jack creates and funds it: kalshi_subaccount.py; --setup prints
the steps). The bot refuses subaccount 0, a subaccount with cash under
FPROBE_MIN_CASH, and one holding orders or positions this bot never made.
Every order: 1 contract, post-only, GTC with an exchange-side expiry at
t0 + hold + 120 s (a dead bot leaves nothing resting for long), read back for
its subaccount_number (a mismatch writes HALT). Caps (UTC day): FPROBE_MAX_
PROBES_DAY 150 orders, FPROBE_MAX_FILLS_DAY 60 contracts, FPROBE_MAX_HELD_COST
$40 of positions at cost. A filled contract is held to settlement.
HALT file (run-logs/follow-probe/HALT): the bot cancels its orders, ends its
trials and idles while it exists.

    python follow_probe.py                      # dry run (no-order baseline), until Ctrl-C
    python follow_probe.py --until 2026-11-09T02:00Z
    python follow_probe.py --scan               # one universe scan (read-only)
    python follow_probe.py --report [--since 2026-11-08] [--until ...]
    python follow_probe.py --live --subaccount N
    python follow_probe.py --cancel-all --subaccount N
    python follow_probe.py --setup              # the subaccount steps (Jack's)

Knobs: FPROBE_<FIELD> for every Config field (FPROBE_HOLD_S=300 ...).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

VERSION = "3"   # 3: restarts restore caps + cooldowns; staggered starts; picks rows (2: no control twin; v1 rows carry "pair")
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FPROBE_DATA", os.path.join(HERE, "run-logs", "follow-probe"))
# orders and subaccount reads: Kalshi's production host (nfl_snipe_bot's finding)
API_BASE = os.environ.get("FPROBE_API_BASE", "https://external-api.kalshi.com/trade-api/v2")
WS_URL = os.environ.get("FPROBE_WS_URL", "wss://api.elections.kalshi.com/trade-api/ws/v2")
WS_PATH = "/trade-api/ws/v2"
CLIENT_PREFIX = "fpb"
EXCLUDE_SERIES = ("KXTEMPMIAH",)          # temp_mm_live's series (subaccount 2)
EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_HALTED = 0, 1, 2, 3
EPS = 1e-6
PX_TOL = 0.005                            # cents: two prices are one level


# ---------------------------------------------------------------- config

@dataclass
class Config:
    min_spread_c: float = 10.0       # a "wide book": ask - bid >= this
    # ...and under this (0 = no cap). 11/08 scan: 282 of 453 wide reward books
    # were 40c+ stubs (9/95, 60/99) whose mid is no theoretical price; a 1-lot
    # at their mid is a free option for whoever knows where the market trades
    max_spread_c: float = 40.0
    min_mid_c: float = 3.0
    max_mid_c: float = 97.0
    edge_c: float = 0.0              # back the theoretical price off by this
    side: str = "random"             # random | bid | ask
    pre_s: float = 60.0
    hold_s: float = 600.0
    post_s: float = 300.0
    debounce_s: float = 1.0          # an event counts once it stood this long
    book_wait_s: float = 60.0        # a book that never arrives: abandon
    max_open: int = 6                # probes running at once
    # probes started per 5 s start tick: a wide max_open ramps up over a minute
    # or two, so its places (and the cancels 10 min later) never burst on the
    # API key the IMM shares
    start_burst: int = 4
    max_probes_day: int = 150        # probes started per UTC day (both modes)
    max_fills_day: int = 60          # live: contracts filled per UTC day
    max_held_cost: float = 40.0      # live: $ of positions at cost
    min_cash: float = 5.0            # live: the subaccount's cash floor
    market_cooldown_h: float = 24.0  # a market is used once per this
    event_gap_min: float = 30.0      # an event rests this long between trials
    min_close_h: float = 3.0         # the market must close this far out
    active_share: float = 0.5        # probes drawn from the "active" stratum
    scan_every_s: float = 600.0
    scan_max_age_s: float = 1800.0   # no new probes on an older scan
    imm_exclude: str = "event"       # event | market (primary resting orders)
    max_unavail_frac: float = 0.1    # report: book unreadable this share of hold
    ws_stale_s: float = 30.0
    tick_s: float = 0.5
    reconcile_s: float = 300.0
    seed: int = 0                    # 0 = a fresh random seed (logged)

    @property
    def trial_s(self) -> float:
        return self.pre_s + self.hold_s + self.post_s

    def validate(self) -> "Config":
        if self.side not in ("random", "bid", "ask"):
            raise ValueError(f"FPROBE_SIDE must be random|bid|ask, not {self.side!r}")
        if self.imm_exclude not in ("event", "market"):
            raise ValueError(f"FPROBE_IMM_EXCLUDE must be event|market, not {self.imm_exclude!r}")
        if self.min_spread_c < 2:
            raise ValueError("FPROBE_MIN_SPREAD_C must be >= 2 (room for a price inside)")
        if 0 < self.max_spread_c <= self.min_spread_c:
            raise ValueError("FPROBE_MAX_SPREAD_C must be over MIN_SPREAD_C (or 0 = no cap)")
        if self.hold_s <= 0 or self.pre_s < 0 or self.post_s < 0:
            raise ValueError("FPROBE_HOLD_S must be > 0, PRE_S / POST_S >= 0")
        if not 0.0 <= self.active_share <= 1.0:
            raise ValueError("FPROBE_ACTIVE_SHARE must be in [0, 1]")
        if self.start_burst < 1 or self.max_open < 1:
            raise ValueError("FPROBE_START_BURST and FPROBE_MAX_OPEN must be >= 1")
        return self

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "Config":
        env = os.environ if environ is None else environ
        kw: Dict[str, Any] = {}
        for f in fields(cls):
            raw = env.get("FPROBE_" + f.name.upper())
            if raw is None or str(raw).strip() == "":
                continue
            d = f.default
            kw[f.name] = type(d)(float(raw)) if isinstance(d, int) else type(d)(raw)
        return cls(**kw).validate()


# ---------------------------------------------------------------- helpers

def utc_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts,
                                  tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_day(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts,
                                  tz=timezone.utc).strftime("%Y-%m-%d")


def parse_iso(v: Any) -> Optional[float]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def fpf(x: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def cents(dollars: Any) -> Optional[float]:
    v = fpf(dollars, None)
    return None if v is None else round(v * 100.0, 2)


def levels_cents(levels: Dict[float, float]) -> Dict[float, float]:
    """A KalshiFeed side ({dollars: size}) -> {cents: size}."""
    return {round(p * 100.0, 2): float(q) for p, q in levels.items() if q > EPS}


def series_of(event: str) -> str:
    return str(event or "").split("-")[0]


def best(levels: Dict[float, float]) -> Optional[float]:
    return max((p for p, q in levels.items() if q > EPS), default=None)


def own_view(yes_c: Dict[float, float], no_c: Dict[float, float],
             side: str) -> Tuple[Dict[float, float], float]:
    """(our side's levels, the other side's best) in OWN-PRICE space, where
    higher is better for us. A bid probe's side is the YES bids and the other
    side's best is the YES ask (100 - best NO bid). An ask probe sells YES at
    P, which rests as a NO bid at 100 - P: its side is the NO bids, the other
    side's best is 100 - best YES bid. 100.0 = nothing on the other side."""
    own, other = (yes_c, no_c) if side == "bid" else (no_c, yes_c)
    ob = best(other)
    return dict(own), (100.0 if ob is None else round(100.0 - ob, 2))


def theo_x(own_best: Optional[float], opp: float, cfg: Config) -> Optional[float]:
    """The theoretical price in own space: floor(mid - edge), strictly inside
    the spread (post-only can rest there). None when it can't."""
    if own_best is None or opp >= 100.0 - EPS:
        return None
    x = float(math.floor((own_best + opp) / 2.0 - cfg.edge_c + 1e-9))
    if x <= own_best + EPS or x >= opp - EPS or x < 1 or x > 99:
        return None
    return x


def yes_price_c(side: str, x: float) -> float:
    """Own-space price -> the order's YES price in cents."""
    return x if side == "bid" else round(100.0 - x, 2)


def level_at(levels: Dict[float, float], x: float) -> float:
    return sum(q for p, q in levels.items() if abs(p - x) < PX_TOL)


def program_info(progs: List[dict]) -> dict:
    """A market's active liquidity programs -> {reward_day, target, df, end_ts}."""
    out = {"reward_day": 0.0, "target": None, "df": None, "end_ts": None, "n": len(progs)}
    for p in progs:
        st, en = parse_iso(p.get("start_date")), parse_iso(p.get("end_date"))
        pr = fpf(p.get("period_reward"), 0.0) / 10000.0          # centi-cents -> $
        if st and en and en > st:
            out["reward_day"] += pr / ((en - st) / 86400.0)
        tgt = fpf(p.get("target_size_fp"), None)
        if tgt:
            out["target"] = tgt if out["target"] is None else min(out["target"], tgt)
        dfb = fpf(p.get("discount_factor_bps"), None)
        if dfb is not None:
            out["df"] = dfb / 10000.0
        if en:
            out["end_ts"] = en if out["end_ts"] is None else max(out["end_ts"], en)
    out["reward_day"] = round(out["reward_day"], 2)
    return out


# ---------------------------------------------------------------- universe

@dataclass
class Cand:
    ticker: str
    event: str
    bid_c: float
    ask_c: float
    vol24h: float = 0.0
    oi: float = 0.0
    close_ts: Optional[float] = None
    last_c: Optional[float] = None
    prog: dict = field(default_factory=dict)

    @property
    def series(self) -> str:
        return series_of(self.event)

    @property
    def stratum(self) -> str:
        return "active" if self.vol24h > 0 else "quiet"

    def brief(self) -> dict:
        return {"bid_c": self.bid_c, "ask_c": self.ask_c, "vol24h": self.vol24h,
                "oi": self.oi, "close": utc_iso(self.close_ts) if self.close_ts else None,
                "last_c": self.last_c, "prog": self.prog}


def market_reason(m: dict, prog: dict, now: float, cfg: Config) -> Optional[str]:
    """Why a REST market row is not a wide reward book we may probe (None = it is)."""
    if str(m.get("status") or "") not in ("active", "open"):
        return "status"
    if int(fpf(m.get("exchange_index"), 0) or 0) != 0:
        return "shard"
    if series_of(m.get("event_ticker", "")) in EXCLUDE_SERIES:
        return "series"
    b, a = cents(m.get("yes_bid_dollars")), cents(m.get("yes_ask_dollars"))
    if b is None or b < 1 or a is None or a <= 0 or a > 99:
        return "one_sided"
    if a - b < cfg.min_spread_c - EPS:
        return "tight"
    if cfg.max_spread_c > 0 and a - b >= cfg.max_spread_c - EPS:
        return "stub"
    if not cfg.min_mid_c <= (a + b) / 2.0 <= cfg.max_mid_c:
        return "mid"
    close = parse_iso(m.get("close_time"))
    if close is not None and close < now + cfg.min_close_h * 3600:
        return "closing"
    if prog.get("end_ts") and prog["end_ts"] < now + cfg.trial_s + 300:
        return "program_ending"
    return None


@dataclass
class ScanResult:
    ts: float
    cands: List[Cand]
    excl_events: Set[str]
    excl_markets: Set[str]
    stats: dict


def build_scan(progs: List[dict], markets: List[dict], primary_orders: List[dict],
               now: float, cfg: Config) -> ScanResult:
    by_t: Dict[str, List[dict]] = {}
    for p in progs:
        if str(p.get("incentive_type") or "liquidity") == "liquidity" and p.get("market_ticker"):
            by_t.setdefault(p["market_ticker"], []).append(p)
    ev_of = {m.get("ticker"): m.get("event_ticker") for m in markets}
    excl_markets = {o.get("ticker") for o in primary_orders if o.get("ticker")}
    excl_events = {o.get("event_ticker") for o in primary_orders if o.get("event_ticker")}
    excl_events |= {ev_of[t] for t in excl_markets if ev_of.get(t)}
    reasons: Dict[str, int] = {}
    cands: List[Cand] = []
    for m in markets:
        t = m.get("ticker")
        if t not in by_t:
            continue
        pi = program_info(by_t[t])
        why = market_reason(m, pi, now, cfg)
        if why is None and t in excl_markets:
            why = "imm_market"
        if why is None and cfg.imm_exclude == "event" and m.get("event_ticker") in excl_events:
            why = "imm_event"
        if why:
            reasons[why] = reasons.get(why, 0) + 1
            continue
        lp = cents(m.get("last_price_dollars"))
        cands.append(Cand(ticker=t, event=m.get("event_ticker") or "",
                          bid_c=cents(m.get("yes_bid_dollars")) or 0.0,
                          ask_c=cents(m.get("yes_ask_dollars")) or 0.0,
                          vol24h=fpf(m.get("volume_24h_fp"), 0.0) or 0.0,
                          oi=fpf(m.get("open_interest_fp"), 0.0) or 0.0,
                          close_ts=parse_iso(m.get("close_time")),
                          last_c=lp if lp else None, prog=pi))
    stats = {"programs": len(progs), "program_markets": len(by_t), "market_rows": len(markets),
             "eligible": len(cands), "events": len({c.event for c in cands}),
             "active": sum(1 for c in cands if c.stratum == "active"),
             "primary_orders": len(primary_orders), "skipped": reasons}
    return ScanResult(ts=now, cands=cands, excl_events=excl_events,
                      excl_markets=excl_markets, stats=stats)


def pick_probes(cands: List[Cand], n: int, busy_events: Set[str], cooled: Set[str],
                gap_events: Set[str], cfg: Config, rng: random.Random
                ) -> List[Tuple[Cand, str, str]]:
    """Up to n (market, side, stratum): one market per event, the stratum
    by active_share, the side by a coin flip."""
    pool: Dict[str, List[Cand]] = {}
    for c in cands:
        if c.event in busy_events or c.event in gap_events or c.ticker in cooled:
            continue
        pool.setdefault(c.event, []).append(c)
    strata: Dict[str, List[Cand]] = {"active": [], "quiet": []}
    for ev in sorted(pool):
        c = rng.choice(sorted(pool[ev], key=lambda k: k.ticker))
        strata[c.stratum].append(c)
    out: List[Tuple[Cand, str, str]] = []
    for _ in range(max(0, int(n))):
        want = "active" if rng.random() < cfg.active_share else "quiet"
        s = next((s for s in (want, "quiet" if want == "active" else "active")
                  if strata[s]), None)
        if s is None:
            break
        lst = strata[s]
        c = lst.pop(rng.randrange(len(lst)))
        side = cfg.side if cfg.side != "random" else ("bid" if rng.random() < 0.5 else "ask")
        out.append((c, side, s))
    return out


# ---------------------------------------------------------------- one trial

class Held:
    """A value that counts once it has stood `secs` unchanged: see() returns
    the time it began, once, when it first qualifies."""
    __slots__ = ("secs", "v", "since", "done")

    def __init__(self, secs: float):
        self.secs = float(secs)
        self.v: Any = None
        self.since: Optional[float] = None
        self.done = False

    def see(self, v: Any, now: float) -> Optional[float]:
        if self.since is None or v != self.v:
            self.v, self.since, self.done = v, now, False
        if not self.done and now - self.since >= self.secs - 1e-9:
            self.done = True
            return self.since
        return None


HOLD_EVENTS = ("join", "improve", "follow", "step", "opp_step")


class Trial:
    """One probe: sub -> pre -> hold -> post -> done (or abandoned). Live, it
    rests 1 contract at x during hold; dry, it only measures against x."""

    def __init__(self, tid: str, cand: Cand, side: str, stratum: str, now: float,
                 cfg: Config, live: bool):
        self.tid, self.c = tid, cand
        self.side, self.stratum, self.cfg, self.live = side, stratum, cfg, live
        self.phase = "sub"
        self.t_sub = now
        self.t_pre: Optional[float] = None
        self.t0: Optional[float] = None
        self.t1: Optional[float] = None
        self.t_end: Optional[float] = None
        self.reason: Optional[str] = None
        self.x: Optional[float] = None
        self.b0: Optional[float] = None
        self.a0: Optional[float] = None
        self.depth_b0 = 0.0
        self.book0: dict = {}
        # our order (live probe)
        self.our_rem = 0.0
        self.coid: Optional[str] = None
        self.order_id: Optional[str] = None
        self.placed = False
        self.place_err: Optional[str] = None
        self.gone_at: Optional[float] = None     # cancel / 404 / fill known
        self.cancel_pending = False
        self.cancel_err: Optional[str] = None
        self.fill_ts: Optional[float] = None
        self.fill_n = 0.0
        self.fill_px: Optional[float] = None
        # our key's OTHER bots on this market (IMM, crypto): tagged deltas
        self.own_key_deltas = 0
        self.own_key_first: Optional[float] = None
        self.own_key_prefixes: Set[str] = set()
        self.probe_deltas = 0
        # pre
        self.pre_first: Optional[float] = None
        self.pre_last: Optional[float] = None
        self.pre_moves = 0
        self.pre_up = False
        self.pre_down = False
        # hold
        self.ev: Dict[str, float] = {}
        self.raw: Dict[str, float] = {}
        self._held = {k: Held(cfg.debounce_s) for k in HOLD_EVENTS}
        self._hbest = Held(cfg.debounce_s)
        self._hopp = Held(cfg.debounce_s)
        self._hsize = Held(cfg.debounce_s)
        self.best_max: Optional[float] = None
        self.opp_min: Optional[float] = None
        self.follow_size_max = 0.0
        self.improve_by_max = 0.0
        self._last: Optional[Tuple[Optional[float], float, float, float]] = None
        self.t1_best: Optional[float] = None
        self.t1_opp: Optional[float] = None
        self.t1_size = 0.0
        # post
        self._gone = Held(cfg.debounce_s)
        self.gone_s: Optional[float] = None
        self.post_best_end: Optional[float] = None
        self.post_opp_end: Optional[float] = None
        # book availability during hold
        self.unavail_s = 0.0
        self._obs_ts: Optional[float] = None
        self._obs_ok: Optional[bool] = None
        self._tl_last: Any = None

    @property
    def ticker(self) -> str:
        return self.c.ticker

    @property
    def active(self) -> bool:
        return self.phase in ("sub", "pre", "hold", "post")

    def others(self, own: Dict[float, float]) -> Dict[float, float]:
        """Our side's book without our own resting contract."""
        out = dict(own)
        if self.our_rem > EPS and self.x is not None:
            for p in list(out):
                if abs(p - self.x) < PX_TOL:
                    q = out[p] - self.our_rem
                    if q > EPS:
                        out[p] = q
                    else:
                        out.pop(p)
        return out

    def observe(self, now: float, lv: Optional[Tuple[Dict[float, float], Dict[float, float]]],
                timeline: Optional[Callable[[dict], None]] = None) -> None:
        """One look at the book ({cents: size} YES bids, NO bids), or None
        when the feed can't serve it. Call on every change and every tick."""
        if self.phase == "hold" and self._obs_ok is False and self._obs_ts is not None:
            self.unavail_s += max(0.0, now - self._obs_ts)
        self._obs_ts, self._obs_ok = now, lv is not None
        if lv is None or self.phase not in ("pre", "hold", "post"):
            return
        own, opp = own_view(lv[0], lv[1], self.side)
        if self.phase == "pre":
            b = best(own)
            if self.pre_first is None:
                self.pre_first = b
            elif b != self.pre_last:
                self.pre_moves += 1
            if b is not None and self.pre_first is not None:
                self.pre_up = self.pre_up or b > self.pre_first + EPS
                self.pre_down = self.pre_down or b < self.pre_first - EPS
            self.pre_last = b
            return
        oth = self.others(own)
        ob = best(oth)
        at_x = level_at(oth, self.x)
        above = sum(q for p, q in oth.items() if p > self.x + PX_TOL)
        self._last = (ob, opp, at_x, above)
        state = (self.phase, ob, opp, round(at_x, 2), round(above, 2), round(self.our_rem, 2))
        if timeline is not None and state != self._tl_last:
            self._tl_last = state
            timeline({"tid": self.tid, "t": round(now, 3), "ph": self.phase, "ob": ob,
                      "opp": opp, "ax": round(at_x, 2), "ab": round(above, 2),
                      "our": round(self.our_rem, 2)})
        if self.phase == "hold":
            self._hold_obs(now, ob, opp, at_x, above)
        else:
            self._post_obs(now, ob, opp, at_x, above)

    def _hold_obs(self, now: float, ob: Optional[float], opp: float,
                  at_x: float, above: float) -> None:
        conds = {"join": at_x > EPS, "improve": above > EPS, "follow": at_x + above > EPS,
                 "step": ob is not None and self.b0 is not None and ob > self.b0 + EPS,
                 "opp_step": self.a0 is not None and opp < self.a0 - EPS}
        for k, v in conds.items():
            if v and k not in self.raw:
                self.raw[k] = round(now - self.t0, 3)
            s = self._held[k].see(v, now)
            if s is not None and v and k not in self.ev:
                self.ev[k] = round(max(0.0, s - self.t0), 3)
        if self._hbest.see(ob, now) is not None and ob is not None:
            self.best_max = ob if self.best_max is None else max(self.best_max, ob)
            if ob > self.x + EPS:
                self.improve_by_max = max(self.improve_by_max, round(ob - self.x, 2))
        if self._hopp.see(opp, now) is not None:
            self.opp_min = opp if self.opp_min is None else min(self.opp_min, opp)
        size = round(at_x + above, 2)
        if self._hsize.see(size, now) is not None:
            self.follow_size_max = max(self.follow_size_max, size)

    def _post_obs(self, now: float, ob: Optional[float], opp: float,
                  at_x: float, above: float) -> None:
        gone = at_x + above <= EPS
        s = self._gone.see(gone, now)
        if s is not None and gone and self.gone_s is None and self.t1_size > EPS:
            self.gone_s = round(max(0.0, s - self.t1), 3)
        self.post_best_end, self.post_opp_end = ob, opp

    def start_hold(self, now: float, own: Dict[float, float], opp: float, x: float) -> None:
        self.phase, self.t0, self.x = "hold", now, x
        self.b0, self.a0 = best(own), opp
        self.depth_b0 = round(own.get(self.b0, 0.0), 2) if self.b0 is not None else 0.0
        top = sorted(((p, q) for p, q in own.items() if q > EPS), reverse=True)[:5]
        self.book0 = {"own_top": [[p, round(q, 2)] for p, q in top], "opp": opp}

    def start_post(self, now: float) -> None:
        if self._last is not None:
            ob, opp, at_x, above = self._last
            self.t1_best, self.t1_opp, self.t1_size = ob, opp, round(at_x + above, 2)
        self.phase, self.t1 = "post", now

    def row(self, mode: str, run_id: str) -> dict:
        def gap(v: Optional[float]) -> Optional[float]:
            if v is None or self.x is None or self.b0 is None or self.x - self.b0 <= EPS:
                return None
            return round((v - self.b0) / (self.x - self.b0), 3)
        opp_gap = None
        if self.opp_min is not None and self.a0 is not None and self.x is not None \
                and self.a0 - self.x > EPS and self.a0 < 100.0:
            opp_gap = round((self.a0 - self.opp_min) / (self.a0 - self.x), 3)
        revert = None
        if self.t1_best is not None and self.b0 is not None and self.post_best_end is not None \
                and self.t1_best > self.b0 + EPS:
            revert = round((self.t1_best - self.post_best_end) / (self.t1_best - self.b0), 3)
        hold = self.cfg.hold_s
        return {
            "v": VERSION, "run": run_id, "mode": mode, "tid": self.tid, "arm": "probe",
            "status": "done" if self.phase == "done" else "abandoned",
            "reason": self.reason, "ticker": self.ticker, "event": self.c.event,
            "series": self.c.series, "side": self.side, "stratum": self.stratum,
            "scan": self.c.brief(),
            "t_sub": utc_iso(self.t_sub), "t0": utc_iso(self.t0) if self.t0 else None,
            "t1": utc_iso(self.t1) if self.t1 else None,
            "t_end": utc_iso(self.t_end) if self.t_end else None,
            "hold_s": hold, "post_s": self.cfg.post_s, "debounce_s": self.cfg.debounce_s,
            "x": self.x, "yes_px": yes_price_c(self.side, self.x) if self.x is not None else None,
            "b0": self.b0, "a0": self.a0, "spread0": (round(self.a0 - self.b0, 2)
                                                     if self.a0 is not None and self.b0 is not None
                                                     else None),
            "depth_b0": self.depth_b0, "book0": self.book0,
            "pre": {"moves": self.pre_moves, "up": self.pre_up, "down": self.pre_down,
                    "first": self.pre_first, "last": self.pre_last},
            "ev": dict(self.ev), "raw": dict(self.raw),
            "best_max": self.best_max, "gap_closed": gap(self.best_max),
            "opp_min": self.opp_min, "opp_gap_closed": opp_gap,
            "follow_size_max": self.follow_size_max, "improve_by_max": self.improve_by_max,
            "t1_best": self.t1_best, "t1_opp": self.t1_opp, "t1_size": self.t1_size,
            "gone_s": self.gone_s, "post_best_end": self.post_best_end,
            "post_opp_end": self.post_opp_end, "revert": revert,
            "unavail_s": round(self.unavail_s, 2),
            "unavail_frac": round(self.unavail_s / hold, 4) if hold > 0 else 0.0,
            "own_key_deltas": self.own_key_deltas,
            "own_key_prefixes": sorted(self.own_key_prefixes),
            "probe_deltas": self.probe_deltas,
            "placed": self.placed, "order_id": self.order_id, "coid": self.coid,
            "place_err": self.place_err, "cancel_err": self.cancel_err,
            "fill_s": (round(self.fill_ts - self.t0, 3)
                       if self.fill_ts is not None and self.t0 is not None else None),
            "fill_n": self.fill_n, "fill_px": self.fill_px,
        }


# ---------------------------------------------------------------- Kalshi I/O

class Exchange:
    """Kalshi REST for the probe. Market data goes through kalshi_reads (the
    IMM's signed client, paced per process); the subaccount's reads and every
    write through ExchangeClient on the production host with the same key.
    WRITES REFUSE unless built with live=True (main() does that only after
    --live --subaccount N and the preflight)."""

    def __init__(self, subaccount: int = 0, live: bool = False, api_base: str = API_BASE):
        self.sub = int(subaccount)
        self.live = bool(live)
        self.api_base = api_base
        self._acct: Any = None
        self._lock = threading.Lock()

    def acct(self) -> Any:
        with self._lock:
            if self._acct is None:
                os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")
                import kalshi_reads as kr
                from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient
                c = ExchangeClient(exchange_api_base=self.api_base, key_id=kr.KEY_ID,
                                   private_key=kr.load_private_key())
                c.http_timeout = (3.0, 8.0)
                self._acct = c
            return self._acct

    def ws_headers(self) -> Callable[[], Dict[str, str]]:
        c = self.acct()

        def headers() -> Dict[str, str]:
            ts = str(int(time.time() * 1000))
            return {"KALSHI-ACCESS-KEY": c.key_id,
                    "KALSHI-ACCESS-SIGNATURE": c.sign_pss_text(ts + "GET" + WS_PATH),
                    "KALSHI-ACCESS-TIMESTAMP": ts}
        return headers

    # -- market data (signed first, paced) --
    @staticmethod
    def _md(path: str, params: dict) -> Any:
        from kalshi_reads import kalshi_get
        return kalshi_get(path, params) or {}

    def _md_paged(self, path: str, params: dict, key: str, max_pages: int = 50) -> List[dict]:
        out: List[dict] = []
        cursor = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            js = self._md(path, p)
            items = js.get(key) or []
            out += items
            cursor = js.get("cursor") or js.get("next_cursor")
            if not cursor or not items:
                break
        return out

    def programs(self) -> List[dict]:
        return self._md_paged("/incentive_programs", {"status": "active", "limit": 1000},
                              "incentive_programs")

    def markets(self, tickers: List[str]) -> List[dict]:
        out: List[dict] = []
        for i in range(0, len(tickers), 100):
            js = self._md("/markets", {"tickers": ",".join(tickers[i:i + 100]), "limit": 1000})
            out += js.get("markets") or []
        return out

    def primary_resting(self, ticker: Optional[str] = None) -> List[dict]:
        """The PRIMARY account's resting orders (the IMM, the crypto bots):
        kalshi_reads signs with the account key and sends no subaccount."""
        p: Dict[str, Any] = {"status": "resting", "limit": 1000}
        if ticker:
            p["ticker"] = ticker
        return self._md_paged("/portfolio/orders", p, "orders", max_pages=20)

    # -- the subaccount --
    def _sub(self, params: Optional[dict] = None) -> dict:
        p = dict(params or {})
        if self.sub:
            p["subaccount"] = self.sub
        return p

    def _acct_paged(self, path: str, params: dict, key: str, max_pages: int = 20) -> List[dict]:
        out: List[dict] = []
        cursor = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            js = self.acct().get(path, p) or {}
            items = js.get(key) or []
            out += items
            cursor = js.get("cursor")
            if not cursor or not items:
                break
        return out

    def balance(self) -> float:
        js = self.acct().get("/portfolio/balance", self._sub()) or {}
        return float(fpf(js.get("balance_dollars"), 0.0) or 0.0)

    def sub_resting(self, ticker: Optional[str] = None) -> List[dict]:
        p = self._sub({"status": "resting", "limit": 1000})
        if ticker:
            p["ticker"] = ticker
        return self._acct_paged("/portfolio/orders", p, "orders")

    def sub_positions(self) -> List[dict]:
        rows = self._acct_paged("/portfolio/positions", self._sub({"limit": 1000}),
                                "market_positions")
        return [r for r in rows if abs(fpf(r.get("position_fp", r.get("position")), 0.0) or 0.0) > EPS]

    def sub_fills(self, min_ts: float) -> List[dict]:
        return self._acct_paged("/portfolio/fills", self._sub({"min_ts": int(min_ts), "limit": 1000}),
                                "fills")

    def order_subaccount(self, oid: str, tries: int = 2) -> Optional[int]:
        """subaccount_number on the order record (GetOrder is not
        subaccount-scoped), None when unreadable."""
        for i in range(tries):
            if i:
                time.sleep(0.5)
            try:
                js = self.acct().get(f"/portfolio/orders/{oid}", {}) or {}
            except Exception:                                   # noqa: BLE001
                continue
            o = js.get("order") or js
            if o.get("subaccount_number") is not None:
                return int(o["subaccount_number"])
        return None

    # -- writes --
    def _w(self) -> Any:
        if not self.live:
            raise RuntimeError("writes are disabled (dry run)")
        if self.sub <= 0:
            raise RuntimeError("writes need a numbered subaccount")
        return self.acct()

    def create(self, ticker: str, side: str, x: float, coid: str, expiration_ts: int) -> str:
        """1 contract, post-only, GTC with an exchange-side expiry. side 'bid'
        buys YES at x; 'ask' sells YES at 100 - x (buys NO at x). Returns the
        order id; raises on a reject."""
        c = self._w()
        kw: Dict[str, Any] = dict(ticker=ticker, action="buy", count=1, type="limit",
                                  post_only=True, expiration_ts=int(expiration_ts),
                                  client_order_id=coid,
                                  self_trade_prevention_type="taker_at_cross")
        if side == "bid":
            kw.update(side="yes", yes_price=int(round(x)))
        else:
            kw.update(side="no", no_price=int(round(x)))
        body = c._build_v2_order_body(**kw)
        body["subaccount"] = self.sub
        js = c.post(path=c.events_orders_url, body=json.dumps(body)) or {}
        oid = js.get("order_id") or (js.get("order") or {}).get("order_id") or ""
        if not oid:
            raise RuntimeError(f"no order_id in the create response: {json.dumps(js)[:200]}")
        return str(oid)

    def cancel(self, oid: str, ticker: str, subaccount: Optional[int] = None) -> str:
        """'ok', or 'gone' on a 404 (filled, expired or already cancelled)."""
        c = self._w() if subaccount is None else self.acct()
        sub = self.sub if subaccount is None else int(subaccount)
        params: Dict[str, Any] = {"exchange_index": -1, "market_ticker": ticker}
        if sub:
            params["subaccount"] = sub
        try:
            c.delete(path=f"{c.events_orders_url}/{oid}", params=params)
            return "ok"
        except Exception as e:                                  # noqa: BLE001
            if getattr(e, "status", None) == 404:
                return "gone"
            raise


def err_text(e: BaseException) -> str:
    s = f"{type(e).__name__}: {e}"
    body = getattr(e, "body", None) or getattr(e, "reason", None)
    if body and str(body) not in s:
        s += f" {body}"
    return s[:300]


# ---------------------------------------------------------------- logging

class Log:
    def __init__(self, log_dir: str, echo: bool = True):
        self.dir = log_dir
        self.echo = echo
        self._lock = threading.Lock()
        os.makedirs(log_dir, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = f"{utc_iso().replace('T', ' ')} [FPROBE] {msg}"
        with self._lock:
            if self.echo:
                try:
                    print(line, flush=True)
                except (UnicodeEncodeError, OSError):
                    pass
            try:
                with open(os.path.join(self.dir, f"probe_{utc_day()}.log"), "a",
                          encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def row(self, name: str, row: dict) -> None:
        with self._lock:
            try:
                with open(os.path.join(self.dir, f"{name}_{utc_day()}.jsonl"), "a",
                          encoding="utf-8") as f:
                    f.write(json.dumps({"ts": utc_iso(), **row}, default=str) + "\n")
            except OSError:
                pass


def write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


class SingleInstance:
    """An exclusive lock on a file for the life of the process."""

    def __init__(self, path: str):
        self.path = path
        self.fh: Any = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            return False
        return True

    def release(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None


# ---------------------------------------------------------------- the feed

def make_feed(ex: Exchange, cfg: Config, live: bool, log: Callable[[str], None],
              our_oids: Set[str]) -> Any:
    """One KalshiFeed (orderbook_delta, plus our fills when live) that also
    surfaces every book delta carrying a client_order_id -- the changes our
    own key caused -- as ("own_delta", body) events."""
    import kalshi_ws

    class ProbeFeed(kalshi_ws.KalshiFeed):
        def _on_book(self, msg: dict, snapshot: bool) -> None:  # feed thread, lock held
            super()._on_book(msg, snapshot)
            if snapshot:
                return
            body = msg.get("msg") or {}
            if body.get("client_order_id") and body.get("market_ticker") in self._want:
                self._push_event("own_delta", dict(body))

    prefix = CLIENT_PREFIX + "-"
    return ProbeFeed(
        WS_URL, ex.ws_headers(), log=lambda s: log(f"ws {s}"),
        fill_filter=lambda f: (str(f.get("client_order_id") or "").startswith(prefix)
                               or str(f.get("order_id") or "") in our_oids),
        want_fills=live, want_order_groups=False, chunk=50,
        stale_secs=cfg.ws_stale_s, ping_secs=5.0)


class Scanner(threading.Thread):
    """Re-reads the universe every scan_every_s on its own thread, so a slow
    scan never delays a cancel."""

    def __init__(self, fetch: Callable[[], ScanResult], every_s: float,
                 log: Callable[[str], None]):
        super().__init__(daemon=True, name="fprobe-scan")
        self.fetch, self.every_s, self.log = fetch, float(every_s), log
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._latest: Optional[ScanResult] = None
        self.errors = 0

    def latest(self) -> Optional[ScanResult]:
        with self._lock:
            return self._latest

    def scan_once(self) -> Optional[ScanResult]:
        t0 = time.time()
        try:
            res = self.fetch()
        except Exception as e:                                  # noqa: BLE001
            self.errors += 1
            self.log(f"! scan failed: {err_text(e)}")
            return None
        with self._lock:
            self._latest = res
        s = res.stats
        self.log(f"scan: {s['programs']} programs / {s['program_markets']} markets -> "
                 f"{s['eligible']} wide reward books in {s['events']} events "
                 f"({s['active']} traded 24h); skipped {s['skipped']} "
                 f"[{time.time() - t0:.1f}s]")
        return res

    def run(self) -> None:
        while not self._stop_evt.is_set():
            self.scan_once()
            if self._stop_evt.wait(self.every_s):
                break

    def stop(self) -> None:
        self._stop_evt.set()


# ---------------------------------------------------------------- the bot

class Bot:
    def __init__(self, cfg: Config, ex: Exchange, feed: Any, log: Log, data_dir: str,
                 live: bool, rng: random.Random, scanner: Optional[Scanner] = None,
                 our_oids: Optional[Set[str]] = None, run_id: Optional[str] = None):
        self.cfg, self.ex, self.feed, self.log = cfg, ex, feed, log
        self.data_dir, self.live, self.rng, self.scanner = data_dir, live, rng, scanner
        self.mode = "live" if live else "dry"
        self.run_id = run_id or uuid.uuid4().hex[:8]
        self.our_oids = our_oids if our_oids is not None else set()
        self.trials: Dict[str, Trial] = {}
        self.by_ticker: Dict[str, Trial] = {}
        self.by_oid: Dict[str, Trial] = {}
        self.cooled: Dict[str, float] = {}           # ticker -> until
        self.event_free: Dict[str, float] = {}       # event -> free from
        self.seq = 0
        self.day = utc_day()
        self.counts = self._zero_counts()
        self.cash: Optional[float] = None
        self.held_cost = 0.0
        self.halted = False
        self.halt_reason = ""
        never = float("-inf")
        self._last_tick = never
        self._last_start = never
        self._last_reconcile = never
        self._last_status = never
        self._last_halt_check = never
        self._last_cap_log = never
        self._fills_seen: Set[str] = set()
        self.t_start = time.time()

    @staticmethod
    def _zero_counts() -> Dict[str, Any]:
        return {"probes": 0, "placed": 0, "fills": 0.0, "done": 0, "abandoned": 0,
                "followed": 0, "joined": 0}

    # -- picking up after a restart --
    def _day_rows(self, name: str, day: str) -> List[dict]:
        out: List[dict] = []
        try:
            with open(os.path.join(self.data_dir, f"{name}_{day}.jsonl"), encoding="utf-8") as f:
                for line in f:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out

    def restore(self, now: float) -> str:
        """Pick up where earlier runs in this mode left off. Today's caps count
        their probes, placed orders and fills, and the markets / events they
        used stay cooled. Without this a restart (by hand, or the launcher after
        a crash) re-probed the same markets within minutes and reset the
        fills-per-day cap. Reads picks / trials / orders / fills of today and
        yesterday; a row without an event ticker (orders before v3) takes the
        ticker minus its last '-' part."""
        cfg = self.cfg
        today = utc_day(now)
        gap = cfg.trial_s + cfg.book_wait_s + cfg.event_gap_min * 60
        tids: Set[str] = set()
        placed, fills = 0, 0.0
        done = {"done": 0, "abandoned": 0, "followed": 0, "joined": 0}

        def cool(ticker: Any, ev: Any, ts: Optional[float]) -> None:
            if not ticker or ts is None:
                return
            t = str(ticker)
            self.cooled[t] = max(self.cooled.get(t, float("-inf")),
                                 ts + cfg.market_cooldown_h * 3600)
            e = str(ev or t.rsplit("-", 1)[0])
            self.event_free[e] = max(self.event_free.get(e, float("-inf")), ts + gap)

        for day in (utc_day(now - 86400), today):
            mine = [r for r in self._day_rows("picks", day) if r.get("mode") == self.mode]
            for r in mine:
                cool(r.get("ticker"), r.get("event_ticker"), parse_iso(r.get("ts")))
            trials = [r for r in self._day_rows("trials", day) if r.get("mode") == self.mode]
            for r in trials:
                cool(r.get("ticker"), r.get("event"), parse_iso(r.get("ts")))
            orders = ([r for r in self._day_rows("orders", day) if r.get("event") == "placed"]
                      if self.live else [])
            for r in orders:
                cool(r.get("ticker"), r.get("event_ticker"), parse_iso(r.get("ts")))
            if day != today:
                continue
            tids |= {str(r["tid"]) for r in mine + trials + orders if r.get("tid")}
            placed = len(orders)
            for r in trials:
                ev = r.get("ev") or {}
                if r.get("status") in ("done", "abandoned"):
                    done[r["status"]] += 1
                done["followed"] += ev.get("follow") is not None
                done["joined"] += ev.get("join") is not None
            if self.live:
                for r in self._day_rows("fills", day):
                    b = r.get("body") or {}
                    key = (str(b.get("trade_id") or b.get("fill_id") or "")
                           or f"{b.get('order_id')}:{b.get('ts')}")
                    if key in self._fills_seen:
                        continue
                    self._fills_seen.add(key)
                    fills += abs(float(fpf(b.get("count_fp", b.get("count")), 0.0) or 0.0))
        c = self.counts
        c["probes"] = max(c["probes"], len(tids))
        c["placed"] = max(c["placed"], placed)
        c["fills"] = max(c["fills"], fills)
        for k, v in done.items():
            c[k] = max(c[k], v)
        n_cool = sum(1 for v in self.cooled.values() if v > now)
        n_gap = sum(1 for v in self.event_free.values() if v > now)
        return (f"restored from today's logs: {c['probes']} probes, {c['placed']} placed, "
                f"{c['fills']:g} filled; {n_cool} market(s) cooling, {n_gap} event(s) resting")

    # -- helpers --
    def _roll_day(self, now: float) -> None:
        d = utc_day(now)
        if d != self.day:
            self.log(f"new UTC day {d}: yesterday {self.counts}")
            self.day, self.counts = d, self._zero_counts()

    def _timeline(self, r: dict) -> None:
        self.log.row("timeline", r)

    def _levels(self, ticker: str) -> Optional[Tuple[Dict[float, float], Dict[float, float]]]:
        lv = self.feed.book_levels(ticker)
        if lv is None:
            return None
        return levels_cents(lv[0]), levels_cents(lv[1])

    def _sync_markets(self) -> None:
        self.feed.set_markets({t.ticker for t in self.trials.values() if t.active})

    def open_count(self) -> int:
        return sum(1 for t in self.trials.values() if t.active)

    # -- starting probes --
    def caps_block(self) -> Optional[str]:
        c = self.cfg
        if self.counts["probes"] >= c.max_probes_day:
            return f"{c.max_probes_day} probes today"
        if self.live:
            if self.counts["fills"] >= c.max_fills_day:
                return f"{c.max_fills_day} contracts filled today"
            if self.held_cost + 1.0 > c.max_held_cost:
                return f"${self.held_cost:.2f} held of the ${c.max_held_cost:.0f} cap"
            if self.cash is not None and self.cash < c.min_cash:
                return f"cash ${self.cash:.2f} under ${c.min_cash:.2f}"
        return None

    def start_probes(self, now: float, scan: Optional[ScanResult]) -> int:
        if self.halted or scan is None or now - scan.ts > self.cfg.scan_max_age_s:
            return 0
        n = self.cfg.max_open - self.open_count()
        if n <= 0:
            return 0
        why = self.caps_block()
        if why:
            if now - self._last_cap_log > 3600:
                self._last_cap_log = now
                self.log(f"no new probes: {why}")
            return 0
        n = min(n, self.cfg.start_burst, self.cfg.max_probes_day - self.counts["probes"])
        busy = {t.c.event for t in self.trials.values() if t.active}
        cooled = {k for k, v in self.cooled.items() if v > now}
        gap = {k for k, v in self.event_free.items() if v > now}
        started = 0
        for cand, side, stratum in pick_probes(scan.cands, n, busy, cooled, gap,
                                               self.cfg, self.rng):
            if not self._primary_clear(cand):
                continue
            self.seq += 1
            tr = Trial(f"{self.run_id}-{self.seq}", cand, side, stratum, now, self.cfg,
                       self.live)
            self.trials[tr.tid] = tr
            self.by_ticker[cand.ticker] = tr
            self.cooled[cand.ticker] = now + self.cfg.market_cooldown_h * 3600
            self.counts["probes"] += 1
            started += 1
            self.log.row("picks", {"run": self.run_id, "tid": tr.tid, "mode": self.mode,
                                   "ticker": cand.ticker, "event_ticker": cand.event,
                                   "side": side, "stratum": stratum})
            self.log(f"probe {tr.tid} ({stratum}, {side}): {cand.ticker} "
                     f"{cand.bid_c:g}/{cand.ask_c:g}")
        if started:
            self._sync_markets()
        return started

    def _primary_clear(self, c: Cand) -> bool:
        """No primary-account order rests on this market right now (the
        scan's read can be up to scan_every_s old)."""
        try:
            rows = self.ex.primary_resting(c.ticker)
        except Exception as e:                                  # noqa: BLE001
            self.log(f"! primary orders read failed on {c.ticker}: {err_text(e)}; skipped")
            return False
        if rows:
            self.log(f"skip {c.ticker}: the primary account rests {len(rows)} order(s) there")
            self.cooled[c.ticker] = time.time() + self.cfg.market_cooldown_h * 3600
            return False
        return True

    # -- events --
    def on_fill(self, body: dict, now: float) -> None:
        oid = str(body.get("order_id") or "")
        tid_key = str(body.get("trade_id") or body.get("fill_id") or "") or f"{oid}:{body.get('ts')}"
        if tid_key in self._fills_seen:
            return
        self._fills_seen.add(tid_key)
        tr = self.by_oid.get(oid)
        n = float(fpf(body.get("count_fp", body.get("count")), 0.0) or 0.0)
        px = cents(body.get("yes_price_dollars"))
        self.counts["fills"] += n
        if tr is not None:
            yes = px if px is not None else yes_price_c(tr.side, tr.x or 0.0)
            cost = (yes if tr.side == "bid" else 100.0 - yes) / 100.0 * n
            self.held_cost += cost
            tr.fill_n += n
            tr.fill_px = yes
            if tr.fill_ts is None:
                tr.fill_ts = now
            tr.our_rem = max(0.0, tr.our_rem - n)
            if tr.our_rem <= EPS:
                tr.gone_at = tr.gone_at or now
            self.log(f"FILL {tr.ticker} {tr.side} {n:g} @ {yes:g}c (trial {tr.tid}, "
                     f"{tr.phase} +{now - (tr.t0 or now):.0f}s)")
        else:
            self.log(f"FILL on an unknown order {oid} ({body.get('market_ticker')})")
        self.log.row("fills", {"run": self.run_id, "order_id": oid,
                               "tid": tr.tid if tr else None, "body": body})

    def on_own_delta(self, body: dict, now: float) -> None:
        tr = self.by_ticker.get(str(body.get("market_ticker") or ""))
        if tr is None or not tr.active:
            return
        coid = str(body.get("client_order_id") or "")
        if coid.startswith(CLIENT_PREFIX + "-"):
            tr.probe_deltas += 1
            d = float(fpf(body.get("delta_fp"), 0.0) or 0.0)
            if d < 0 and coid == tr.coid:
                tr.our_rem = max(0.0, tr.our_rem + d)
            return
        tr.own_key_deltas += 1
        tr.own_key_prefixes.add(coid.split("-")[0][:12])
        if tr.own_key_first is None:
            tr.own_key_first = now
            self.log(f"{tr.ticker}: our own key touched it ({coid.split('-')[0]}, "
                     f"subaccount {body.get('subaccount')}) -- trial {tr.tid} leaves the score")

    # -- phases --
    def advance(self, now: float) -> None:
        for tr in list(self.trials.values()):
            if not tr.active:
                continue
            if tr.gone_at is not None and tr.our_rem > EPS and now - tr.gone_at > 3.0:
                tr.our_rem = 0.0
            if tr.phase == "sub":
                lv = self._levels(tr.ticker)
                if lv is not None:
                    tr.phase, tr.t_pre = "pre", now
                    tr.observe(now, lv)
                elif now - tr.t_sub > self.cfg.book_wait_s:
                    self.abandon(tr, now, "no_book")
            elif tr.phase == "pre":
                if now - (tr.t_pre or now) >= self.cfg.pre_s:
                    self.begin_hold(tr, now)
            elif tr.phase == "hold":
                if now - (tr.t0 or now) >= self.cfg.hold_s:
                    self.begin_post(tr, now)
            elif tr.phase == "post":
                if tr.cancel_pending:
                    self._cancel(tr, now)
                if now - (tr.t1 or now) >= self.cfg.post_s:
                    self.finish(tr, now)

    def begin_hold(self, tr: Trial, now: float) -> None:
        lv = self._levels(tr.ticker)
        if lv is None:
            return self.abandon(tr, now, "no_book_t0")
        if tr.own_key_deltas:
            return self.abandon(tr, now, "own_key_pre")
        own, opp = own_view(lv[0], lv[1], tr.side)
        b = best(own)
        if b is None or opp >= 100.0 - EPS:
            return self.abandon(tr, now, "one_sided_t0")
        if opp - b < self.cfg.min_spread_c - EPS:
            return self.abandon(tr, now, "tightened_t0")
        if self.cfg.max_spread_c > 0 and opp - b >= self.cfg.max_spread_c - EPS:
            return self.abandon(tr, now, "stub_t0")
        x = theo_x(b, opp, self.cfg)
        if x is None:
            return self.abandon(tr, now, "no_price_t0")
        tr.start_hold(now, own, opp, x)
        if self.live:
            if not self._place(tr, now):
                return
        tr.observe(now, lv, self._timeline)
        self.log(f"t0 {tr.ticker} {tr.side}: book {b:g}/{opp:g} (own space) -> "
                 f"{'PLACED' if tr.placed else 'would rest'} 1 @ {x:g} "
                 f"(YES {yes_price_c(tr.side, x):g}c)")

    def _place(self, tr: Trial, now: float) -> bool:
        why = None
        if self.live and self.counts["fills"] >= self.cfg.max_fills_day:
            why = "cap_fills"
        elif self.live and self.held_cost + 1.0 > self.cfg.max_held_cost:
            why = "cap_held"
        if why:
            self.abandon(tr, now, why)
            return False
        self.seq += 1
        tr.coid = f"{CLIENT_PREFIX}-{self.run_id}-{self.seq}"
        tr.our_rem = 1.0              # before the send: its delta can beat the reply
        exp = int(now + self.cfg.hold_s + 120)
        try:
            oid = self.ex.create(tr.ticker, tr.side, tr.x, tr.coid, exp)
        except Exception as e:                                  # noqa: BLE001
            tr.our_rem = 0.0
            tr.place_err = err_text(e)
            self.log.row("orders", {"run": self.run_id, "tid": tr.tid, "event": "reject",
                                    "ticker": tr.ticker, "side": tr.side, "x": tr.x,
                                    "coid": tr.coid, "error": tr.place_err})
            self.log(f"! place {tr.ticker} {tr.side} 1 @ {tr.x:g} refused: {tr.place_err}")
            self.abandon(tr, now, "place_refused")
            return False
        tr.order_id, tr.placed = oid, True
        self.our_oids.add(oid)
        self.by_oid[oid] = tr
        self.counts["placed"] += 1
        self.log.row("orders", {"run": self.run_id, "tid": tr.tid, "event": "placed",
                                "ticker": tr.ticker, "event_ticker": tr.c.event,
                                "side": tr.side, "x": tr.x,
                                "yes_px": yes_price_c(tr.side, tr.x), "coid": tr.coid,
                                "order_id": oid, "expires": utc_iso(exp),
                                "subaccount": self.ex.sub})
        booked = self.ex.order_subaccount(oid)
        if booked is not None and booked != self.ex.sub:
            self._halt(f"order {oid} on {tr.ticker} booked to subaccount {booked}, "
                       f"not {self.ex.sub}", now, booked_sub=booked, tr=tr)
            return False
        return True

    def begin_post(self, tr: Trial, now: float) -> None:
        tr.start_post(now)
        if tr.placed and tr.our_rem > EPS:
            self._cancel(tr, now)
        lv = self._levels(tr.ticker)
        if lv is not None:
            tr.observe(now, lv, self._timeline)

    def _cancel(self, tr: Trial, now: float, subaccount: Optional[int] = None) -> None:
        if not tr.order_id:
            return
        try:
            res = self.ex.cancel(tr.order_id, tr.ticker, subaccount)
            tr.cancel_pending = False
            tr.gone_at = tr.gone_at or now
            self.log.row("orders", {"run": self.run_id, "tid": tr.tid, "event": f"cancel_{res}",
                                    "ticker": tr.ticker, "order_id": tr.order_id})
        except Exception as e:                                  # noqa: BLE001
            tr.cancel_pending = True
            tr.cancel_err = err_text(e)
            self.log(f"! cancel {tr.order_id} on {tr.ticker} failed: {tr.cancel_err} "
                     f"(retrying; the order expires on its own)")

    def finish(self, tr: Trial, now: float) -> None:
        tr.phase, tr.t_end = "done", now
        self._close(tr, now)
        row = tr.row(self.mode, self.run_id)
        self.log.row("trials", row)
        self.counts["done"] += 1
        f = row["ev"].get("follow")
        if f is not None:
            self.counts["followed"] += 1
        if row["ev"].get("join") is not None:
            self.counts["joined"] += 1
        self.log(f"done {tr.ticker}: follow {f if f is not None else '-'}s "
                 f"join {row['ev'].get('join', '-')} improve {row['ev'].get('improve', '-')} "
                 f"step {row['ev'].get('step', '-')} gap {row['gap_closed']} "
                 f"t1 size {row['t1_size']:g} gone {row['gone_s']} "
                 f"{'FILLED ' if tr.fill_n else ''}"
                 f"{'[own key ' + ','.join(row['own_key_prefixes']) + ']' if tr.own_key_deltas else ''}")

    def abandon(self, tr: Trial, now: float, reason: str) -> None:
        if tr.placed and tr.our_rem > EPS and tr.gone_at is None:
            self._cancel(tr, now)
        tr.phase, tr.reason, tr.t_end = "abandoned", reason, now
        self._close(tr, now)
        self.log.row("trials", tr.row(self.mode, self.run_id))
        self.counts["abandoned"] += 1
        self.log(f"abandoned {tr.ticker}: {reason}")

    def _close(self, tr: Trial, now: float) -> None:
        self.event_free[tr.c.event] = now + self.cfg.event_gap_min * 60
        if self.by_ticker.get(tr.ticker) is tr:
            self.by_ticker.pop(tr.ticker, None)
        self.trials.pop(tr.tid, None)
        self._sync_markets()
        if self.live and tr.placed:
            try:
                left = [o for o in self.ex.sub_resting(tr.ticker)
                        if str(o.get("client_order_id") or "").startswith(CLIENT_PREFIX + "-")]
            except Exception as e:                              # noqa: BLE001
                self.log(f"! resting read on {tr.ticker} failed: {err_text(e)}")
                left = []
            for o in left:
                self.log(f"! {o.get('order_id')} still rests on {tr.ticker}: cancelling")
                try:
                    self.ex.cancel(str(o.get("order_id")), tr.ticker)
                except Exception as e:                          # noqa: BLE001
                    self.log(f"! cancel failed: {err_text(e)} (it expires on its own)")

    # -- safety --
    def _halt(self, why: str, now: float, booked_sub: Optional[int] = None,
              tr: Optional[Trial] = None) -> None:
        self.log(f"HALT: {why}")
        try:
            with open(os.path.join(self.data_dir, "HALT"), "a", encoding="utf-8") as f:
                f.write(f"{utc_iso(now)} {why}\n")
        except OSError:
            pass
        if tr is not None and booked_sub is not None:
            self._cancel(tr, now, subaccount=booked_sub)
        self.halted, self.halt_reason = True, why
        self.stop_all(now, "halt")

    def check_halt(self, now: float) -> None:
        hp = os.path.join(self.data_dir, "HALT")
        exists = os.path.exists(hp)
        if exists and not self.halted:
            try:
                why = open(hp, encoding="utf-8").read().strip().splitlines()[-1:] or ["HALT file"]
            except OSError:
                why = ["HALT file"]
            self.halted, self.halt_reason = True, why[0][:200]
            self.log(f"HALT file found ({self.halt_reason}): cancelling, idling")
            self.stop_all(now, "halt")
        elif not exists and self.halted:
            self.halted, self.halt_reason = False, ""
            self.log("HALT file gone: resuming")

    def stop_all(self, now: float, reason: str) -> None:
        for tr in list(self.trials.values()):
            if tr.active:
                self.abandon(tr, now, reason)
        if self.live:
            self.cancel_all_ours()

    def cancel_all_ours(self) -> int:
        n = 0
        try:
            rows = self.ex.sub_resting()
        except Exception as e:                                  # noqa: BLE001
            self.log(f"! resting read failed: {err_text(e)}")
            return 0
        for o in rows:
            if str(o.get("client_order_id") or "").startswith(CLIENT_PREFIX + "-"):
                try:
                    self.ex.cancel(str(o.get("order_id")), str(o.get("ticker")))
                    n += 1
                except Exception as e:                          # noqa: BLE001
                    self.log(f"! cancel {o.get('order_id')} failed: {err_text(e)}")
        if n:
            self.log(f"cancelled {n} resting order(s) of ours")
        return n

    def reconcile(self, now: float) -> None:
        """Live: cash, positions at cost, and any fill the socket missed."""
        if not self.live:
            return
        try:
            self.cash = self.ex.balance()
            self.held_cost = held_cost_of(self.ex.sub_positions())
            for f in self.ex.sub_fills(self.t_start - 60):
                oid = str(f.get("order_id") or "")
                if oid in self.by_oid:
                    self.on_fill({**f, "count_fp": f.get("count_fp", f.get("count"))}, now)
        except Exception as e:                                  # noqa: BLE001
            self.log(f"! reconcile failed: {err_text(e)}")

    def status(self, now: float, scan: Optional[ScanResult]) -> dict:
        st = {"bot": "follow_probe", "v": VERSION, "mode": self.mode, "run": self.run_id,
              "updated": utc_iso(now), "day": self.day, "counts": self.counts,
              "halted": self.halted, "halt_reason": self.halt_reason,
              "subaccount": self.ex.sub, "cash": self.cash, "held_cost": self.held_cost,
              "caps": {"max_probes_day": self.cfg.max_probes_day,
                       "max_fills_day": self.cfg.max_fills_day,
                       "max_held_cost": self.cfg.max_held_cost},
              "active": [{"tid": t.tid, "ticker": t.ticker,
                          "side": t.side, "phase": t.phase, "x": t.x,
                          "follow": t.ev.get("follow"), "placed": t.placed}
                         for t in self.trials.values()],
              "scan": ({"at": utc_iso(scan.ts), **scan.stats} if scan else None)}
        try:
            st["ws"] = self.feed.status()
        except Exception:                                       # noqa: BLE001
            st["ws"] = None
        return st

    # -- the loop --
    def step(self, now: float) -> None:
        self._roll_day(now)
        if now - self._last_halt_check >= 2.0:
            self._last_halt_check = now
            self.check_halt(now)
        dirty, events, dropped = self.feed.drain()
        if dropped:
            self.log(f"! {dropped} feed events dropped")
        for kind, body in events:
            if kind == "fill":
                self.on_fill(body, now)
            elif kind == "own_delta":
                self.on_own_delta(body, now)
        tick = now - self._last_tick >= self.cfg.tick_s
        if tick:
            self._last_tick = now
        for tr in list(self.trials.values()):
            if tr.phase in ("pre", "hold", "post") and (tick or tr.ticker in dirty):
                tr.observe(now, self._levels(tr.ticker), self._timeline)
        self.advance(now)
        if now - self._last_reconcile >= self.cfg.reconcile_s:
            self._last_reconcile = now
            self.reconcile(now)
        scan = self.scanner.latest() if self.scanner else None
        if now - self._last_start >= 5.0:
            self._last_start = now
            self.start_probes(now, scan)
        if now - self._last_status >= 30.0:
            self._last_status = now
            try:
                write_json(os.path.join(self.data_dir, "status.json"), self.status(now, scan))
            except OSError:
                pass

    def run(self, until: Optional[float] = None) -> int:
        self.log(f"follow probe v{VERSION} run {self.run_id}: {self.mode}"
                 f"{' subaccount ' + str(self.ex.sub) if self.live else ' (nothing placed: the no-order baseline)'}"
                 f"; hold {self.cfg.hold_s:g}s pre {self.cfg.pre_s:g}s post {self.cfg.post_s:g}s"
                 f", {self.cfg.max_open} probes at once ({self.cfg.start_burst} started per 5 s)"
                 f", spread {self.cfg.min_spread_c:g}c"
                 f" to {self.cfg.max_spread_c:g}c")
        try:
            while until is None or time.time() < until:
                ev = getattr(self.feed, "event_flag", None)
                if ev is not None:
                    ev.wait(min(0.25, self.cfg.tick_s))
                else:
                    time.sleep(min(0.25, self.cfg.tick_s))
                self.step(time.time())
        except KeyboardInterrupt:
            self.log("interrupted")
        finally:
            now = time.time()
            self.stop_all(now, "shutdown")
            try:
                write_json(os.path.join(self.data_dir, "status.json"),
                           self.status(now, self.scanner.latest() if self.scanner else None))
            except OSError:
                pass
            self.log(f"stopped: {self.counts}")
        return EXIT_HALTED if self.halted else EXIT_OK


def held_cost_of(positions: List[dict]) -> float:
    """$ at cost of the subaccount's open positions: market_exposure_dollars,
    else $1 a contract (an upper bound, so the cap fails safe)."""
    total = 0.0
    for p in positions:
        exp = fpf(p.get("market_exposure_dollars"), None)
        if exp is None:
            exp = abs(fpf(p.get("position_fp", p.get("position")), 0.0) or 0.0)
        total += abs(exp)
    return round(total, 2)


# ---------------------------------------------------------------- live preflight

def placed_tickers(data_dir: str) -> Set[str]:
    out: Set[str] = set()
    try:
        names = os.listdir(data_dir)
    except OSError:
        return out
    for n in names:
        if not (n.startswith("orders_") and n.endswith(".jsonl")):
            continue
        try:
            with open(os.path.join(data_dir, n), encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("event") == "placed" and r.get("ticker"):
                        out.add(r["ticker"])
        except OSError:
            continue
    return out


def live_preflight(ex: Exchange, cfg: Config, data_dir: str,
                   log: Callable[[str], None]) -> Optional[str]:
    """Why --live must not start (None = go). Cancels any order of ours left
    resting by an earlier run."""
    if ex.sub <= 0:
        return "--live needs --subaccount N (a numbered subaccount; never the primary)"
    try:
        cash = ex.balance()
    except Exception as e:                                      # noqa: BLE001
        return f"subaccount {ex.sub}'s balance is unreadable: {err_text(e)}"
    if cash < cfg.min_cash:
        return (f"subaccount {ex.sub} holds ${cash:.2f}, under FPROBE_MIN_CASH "
                f"${cfg.min_cash:.2f}: fund it first (python kalshi_subaccount.py fund "
                f"{ex.sub} 50)")
    try:
        rest = ex.sub_resting()
        pos = ex.sub_positions()
    except Exception as e:                                      # noqa: BLE001
        return f"subaccount {ex.sub}'s orders / positions are unreadable: {err_text(e)}"
    foreign = [o for o in rest
               if not str(o.get("client_order_id") or "").startswith(CLIENT_PREFIX + "-")]
    if foreign:
        return (f"subaccount {ex.sub} has {len(foreign)} resting order(s) this bot did not "
                f"place (e.g. {foreign[0].get('ticker')}): another bot uses it")
    mine = placed_tickers(data_dir)
    stray = [p.get("ticker") for p in pos if p.get("ticker") not in mine]
    if stray:
        return (f"subaccount {ex.sub} holds positions this bot never traded "
                f"({', '.join(map(str, stray[:5]))}): use a fresh subaccount")
    ours = [o for o in rest if o not in foreign]
    for o in ours:
        try:
            ex.cancel(str(o.get("order_id")), str(o.get("ticker")))
        except Exception as e:                                  # noqa: BLE001
            log(f"! cancel of a leftover {o.get('order_id')} failed: {err_text(e)}")
    log(f"preflight: subaccount {ex.sub} cash ${cash:.2f}, {len(pos)} position(s) of ours, "
        f"{len(ours)} leftover order(s) cancelled")
    return None


def fetch_scan(ex: Exchange, cfg: Config) -> ScanResult:
    progs = ex.programs()
    tickers = sorted({p.get("market_ticker") for p in progs if p.get("market_ticker")})
    markets = ex.markets(tickers)
    primary = ex.primary_resting()
    return build_scan(progs, markets, primary, time.time(), cfg)


# ---------------------------------------------------------------- report

def load_rows(data_dir: str, since: Optional[str] = None,
              until: Optional[str] = None) -> List[dict]:
    rows: List[dict] = []
    try:
        names = sorted(os.listdir(data_dir))
    except OSError:
        return rows
    for n in names:
        if not (n.startswith("trials_") and n.endswith(".jsonl")):
            continue
        day = n[len("trials_"):-len(".jsonl")]
        if (since and day < since) or (until and day > until):
            continue
        with open(os.path.join(data_dir, n), encoding="utf-8") as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def usable(r: dict, max_unavail: float = 0.1) -> bool:
    if r.get("status") != "done" or r.get("own_key_deltas"):
        return False
    if (r.get("unavail_frac") or 0.0) > max_unavail:
        return False
    if r.get("mode") == "live" and not r.get("placed"):
        return False
    return True


METRICS: List[Tuple[str, str]] = [
    ("join", "joined AT our price"),
    ("improve", "stepped AHEAD of us"),
    ("follow", "either: someone else at our price or better"),
    ("follow60", "...within 60 s"),
    ("step", "others' best moved up at all"),
    ("opp_step", "the other side came in"),
    ("present_t1", "a follower still there at our cancel"),
]


def metric(r: dict, k: str) -> Optional[float]:
    ev = r.get("ev") or {}
    if k == "follow60":
        return 1.0 if ev.get("follow") is not None and ev["follow"] <= 60 else 0.0
    if k == "present_t1":
        return 1.0 if (r.get("t1_size") or 0.0) > EPS else 0.0
    if k == "gap_closed":
        return r.get("gap_closed")
    return 1.0 if ev.get(k) is not None else 0.0


def mean(xs: List[Optional[float]]) -> Optional[float]:
    vals = [x for x in xs if x is not None]
    return sum(vals) / len(vals) if vals else None


def boot_rate(rows: List[dict], k: str, n: int = 2000,
              seed: int = 7) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """The mean of metric k over rows, with a 90% bootstrap interval."""
    d = [v for v in (metric(r, k) for r in rows) if v is not None]
    if not d:
        return None, None, None
    rng = random.Random(seed)
    m = len(d)
    boots = sorted(sum(d[rng.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return sum(d) / m, boots[int(0.05 * (n - 1))], boots[int(0.95 * (n - 1))]


def quantiles(xs: List[Optional[float]], qs=(0.25, 0.5, 0.75)) -> List[Optional[float]]:
    vals = sorted(x for x in xs if x is not None)
    if not vals:
        return [None for _ in qs]
    return [vals[int(q * (len(vals) - 1))] for q in qs]


def _pct(v: Optional[float]) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _num(v: Optional[float], fmt: str = "{:.0f}") -> str:
    return "-" if v is None else fmt.format(v)


def _count(rows: List[dict], k: str) -> int:
    return sum(1 for r in rows if metric(r, k))


def _of(rows: List[dict], k: str) -> str:
    return f"{_count(rows, k)} of {len(rows)} ({_pct(mean([metric(r, k) for r in rows]))})"


def render_report(rows: List[dict], max_unavail: float = 0.1, title: str = "") -> str:
    live = [r for r in rows if r.get("mode") == "live" and usable(r, max_unavail)]
    base = [r for r in rows if r.get("mode") == "dry" and usable(r, max_unavail)]
    done = [r for r in rows if r.get("status") == "done"]
    aband = [r for r in rows if r.get("status") == "abandoned"]
    own_key = [r for r in done if r.get("own_key_deltas")]
    lines = [f"# Follow probe report{(' -- ' + title) if title else ''}", "",
             f"Trials: {len(rows)} ({len(done)} done, {len(aband)} abandoned). Usable: "
             f"{len(live)} live probes, {len(base)} dry (nothing placed: the no-order "
             f"baseline).", ""]
    if aband:
        rs: Dict[str, int] = {}
        for r in aband:
            rs[str(r.get("reason"))] = rs.get(str(r.get("reason")), 0) + 1
        lines += ["Abandoned: " + ", ".join(f"{k} {v}" for k, v in
                                           sorted(rs.items(), key=lambda kv: -kv[1])), ""]
    if own_key:
        pre: Dict[str, int] = {}
        for r in own_key:
            for p in r.get("own_key_prefixes") or []:
                pre[p] = pre.get(p, 0) + 1
        lines += [f"Left out because one of OUR bots touched the market: {len(own_key)} "
                  f"({', '.join(f'{k} {v}' for k, v in sorted(pre.items()))}).", ""]
    lines += ["## Did other makers follow?", "",
              "| Metric | Live probes | 90% CI | No-order baseline (dry) |", "|---|---|---|---|"]
    for k, label in METRICS:
        v, lo, hi = boot_rate(live, k)
        lines.append(f"| {label} | {_of(live, k) if live else '-'} | {_pct(lo)} .. {_pct(hi)} | "
                     f"{_of(base, k) if base else '-'} |")
    lines += ["", f"Mean share of the gap to our price that others' best closed: live "
              f"{_num(mean([r.get('gap_closed') for r in live]), '{:.2f}')}, baseline "
              f"{_num(mean([r.get('gap_closed') for r in base]), '{:.2f}')}.", ""]
    if not live:
        lines += ["No live probes yet."]
        return "\n".join(lines) + "\n"
    fol = [r for r in live if (r.get("ev") or {}).get("follow") is not None]
    ahead = [r.get("improve_by_max") for r in fol if r.get("improve_by_max")]
    lines += ["## The followers", "",
              f"Seconds after we placed to the first follow (25th / median / 75th): "
              f"{' / '.join(_num(v) for v in quantiles([r['ev']['follow'] for r in fol]))} "
              f"(n={len(fol)}). Largest size at or over our price: median "
              f"{_num(quantiles([r.get('follow_size_max') for r in fol])[1], '{:g}')} contracts. "
              f"Stepped ahead by: median {_num(quantiles(ahead)[1], '{:g}')}c.", ""]
    stay = [r for r in live if (r.get("t1_size") or 0.0) > EPS]
    if stay:
        gone = [r for r in stay if r.get("gone_s") is not None]
        lines += [f"After our cancel: {len(gone)} of the {len(stay)} followers still there left "
                  f"within {stay[0].get('post_s', 0):g}s (median "
                  f"{_num(quantiles([r['gone_s'] for r in gone])[1])}s); leaving with us "
                  f"means they were pegged to our order. Others' best fell back "
                  f"{_num(mean([r.get('revert') for r in stay]), '{:.2f}')} of the way to where "
                  f"it started.", ""]
    lines += ["## By group", "", "| Group | Probes | Joined | Ahead | Either | 90% CI |",
              "|---|---|---|---|---|---|"]
    groups: Dict[str, List[dict]] = {}
    for r in live:
        groups.setdefault(f"stratum {r.get('stratum')}", []).append(r)
        groups.setdefault(f"side {r.get('side')}", []).append(r)
        sp = r.get("spread0") or 0
        groups.setdefault("spread " + ("10-19c" if sp < 20 else "20-29c" if sp < 30 else "30c+"),
                          []).append(r)
    for g in sorted(groups):
        rs_ = groups[g]
        v, lo, hi = boot_rate(rs_, "follow")
        lines.append(f"| {g} | {len(rs_)} | {_count(rs_, 'join')} | {_count(rs_, 'improve')} | "
                     f"{_count(rs_, 'follow')} ({_pct(v)}) | {_pct(lo)} .. {_pct(hi)} |")
    ser: Dict[str, List[dict]] = {}
    for r in live:
        ser.setdefault(r.get("series") or "?", []).append(r)
    top = sorted(ser.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:15]
    lines += ["", "## By series (top 15 by probes)", "",
              "| Series | Probes | Joined | Ahead | Median s to follow |", "|---|---|---|---|---|"]
    for s, rs_ in top:
        fs = [r["ev"]["follow"] for r in rs_ if (r.get("ev") or {}).get("follow") is not None]
        lines.append(f"| {s} | {len(rs_)} | {_count(rs_, 'join')} | {_count(rs_, 'improve')} | "
                     f"{_num(quantiles(fs)[1])} |")
    fills = [r for r in live if (r.get("fill_n") or 0) > 0]
    lines += ["", f"## Our fills: {sum(r['fill_n'] for r in fills):g} contract(s) on "
              f"{len(fills)} of {len(live)} probes", ""]
    for r in fills[:20]:
        lines.append(f"- {r['ticker']} {r['side']} {r['fill_n']:g} @ {r.get('fill_px')}c "
                     f"after {_num(r.get('fill_s'))}s")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- CLI

SETUP = """\
Running the probe live in its own Kalshi subaccount (the account owner's steps):
 1. Create a numbered subaccount (prints its number N):
      python kalshi_subaccount.py create
 2. Fund it (positions are 1 contract each, capped at $40 at cost by default):
      python kalshi_subaccount.py fund <N> 50
 3. Start it (dry is the default; --live places real 1-contract orders):
      powershell -ExecutionPolicy Bypass -File run_follow_probe.ps1 -Live -Subaccount <N>
 Stop: create run-logs\\follow-probe\\HALT (cancels and idles), or end the process.
 Take the cash back: python kalshi_subaccount.py withdraw <N> <dollars>
"""


def parse_until(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    ts = parse_iso(v)
    if ts is None:
        raise ValueError(f"--until: not a time: {v!r}")
    return ts


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="place real 1-contract orders")
    ap.add_argument("--subaccount", type=int, default=0)
    ap.add_argument("--until", help="stop at this UTC time (ISO) or unix ts")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--scan", action="store_true", help="one universe scan, then exit")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--until-day", help="--report: last UTC day (YYYY-MM-DD)")
    ap.add_argument("--cancel-all", action="store_true",
                    help="write HALT and cancel every resting order of ours in --subaccount")
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    if a.setup:
        print(SETUP)
        return EXIT_OK
    cfg = Config.from_env()
    data_dir = a.data_dir
    os.makedirs(data_dir, exist_ok=True)

    if a.report:
        rows = load_rows(data_dir, a.since, a.until_day)
        txt = render_report(rows, cfg.max_unavail_frac,
                            title=f"{a.since or 'start'} to {a.until_day or 'now'}")
        out = os.path.join(data_dir, f"report_{utc_day()}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(txt)
        print(f"(written to {out})")
        return EXIT_OK

    log = Log(data_dir, echo=not a.quiet)
    if a.scan:
        ex = Exchange(a.subaccount)
        res = fetch_scan(ex, cfg)
        print(json.dumps(res.stats, indent=1))
        ser: Dict[str, int] = {}
        for c in res.cands:
            ser[c.series] = ser.get(c.series, 0) + 1
        print("by series:", sorted(ser.items(), key=lambda kv: -kv[1])[:30])
        for c in sorted(res.cands, key=lambda c: -c.vol24h)[:15]:
            print(f"  {c.ticker:45s} {c.bid_c:5.1f}/{c.ask_c:5.1f} vol24h {c.vol24h:7.0f} "
                  f"reward ${c.prog.get('reward_day', 0):.2f}/day target {c.prog.get('target')}")
        return EXIT_OK

    if a.cancel_all:
        if a.subaccount <= 0:
            print("--cancel-all needs --subaccount N")
            return EXIT_REFUSED
        with open(os.path.join(data_dir, "HALT"), "a", encoding="utf-8") as f:
            f.write(f"{utc_iso()} --cancel-all\n")
        print(f"HALT written in {data_dir}: a running bot cancels and idles; delete it to resume")
        ex = Exchange(a.subaccount, live=True)
        bot = Bot(cfg, ex, feed=None, log=log, data_dir=data_dir, live=True, rng=random.Random())
        n = bot.cancel_all_ours()
        left = [o for o in ex.sub_resting()
                if str(o.get("client_order_id") or "").startswith(CLIENT_PREFIX + "-")]
        print(f"cancelled {n}; {len(left)} of ours still resting")
        return EXIT_OK if not left else EXIT_ERROR

    lock = SingleInstance(os.path.join(data_dir, "follow_probe.lock"))
    if not lock.acquire():
        print(f"another follow_probe already runs on {data_dir}")
        return EXIT_REFUSED
    try:
        live = bool(a.live)
        ex = Exchange(a.subaccount, live=live)
        if live:
            why = live_preflight(ex, cfg, data_dir, log)
            if why:
                log(f"REFUSED: {why}")
                return EXIT_REFUSED
        seed = cfg.seed or random.SystemRandom().randrange(1, 2 ** 31)
        rng = random.Random(seed)
        our_oids: Set[str] = set()
        feed = make_feed(ex, cfg, live, log, our_oids)
        scanner = Scanner(lambda: fetch_scan(ex, cfg), cfg.scan_every_s, log)
        bot = Bot(cfg, ex, feed, log, data_dir, live, rng, scanner, our_oids)
        log(f"seed {seed}; data {data_dir}")
        log(bot.restore(time.time()))
        feed.start()
        scanner.start()
        if live:
            bot.reconcile(time.time())
        try:
            return bot.run(parse_until(a.until))
        finally:
            scanner.stop()
            feed.stop()
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
