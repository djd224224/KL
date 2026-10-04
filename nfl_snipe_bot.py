#!/usr/bin/env python3
"""NFL player-prop SNIPER -- a taker bot, separate from the IMM.

Jack 2026-10-04: "Build the sniper taker bot separately." The IMM makes
markets on the NFL ladders / escalators and caps its own bids at the
player-history band (nfl_prop_fair). This bot TAKES the other side when a
book sits far outside that band: it sells YES into a bid well over the band
top, or buys YES from an ask well under the band bottom. Every order is
immediate-or-cancel, so nothing it sends ever rests on the book.

THE SIGNAL is nfl_prop_snipe.py's (the read-only scanner this automates).
Backtest on the settled props 9/24-10/1 (the IMM's logged pre-game books,
first signal per market, taker fee deducted):
  sell YES when the bid > band top + 1c   14 markets, won 13, +3.9c/contract
  buy YES when the ask < band bottom      never happened
The losing kind is the move the market makes on NEWS the model does not
have (Keenan Allen out -> Downs' ladder 24 -> 34). A signal is SKIPPED when:
  injury        the player has any ESPN designation (or is off the roster)
  team_news     a contributor on his team changed designation in the last
                news_skip_hours (12) -- this bot's watch or the IMM's (a
                designation CLEARED is only seen by a watch that saw it set;
                the watch's history survives restarts in watch_status.json)
  sibling       his other contract on the same stat (ladder vs escalator)
                implies a mean within 15% of this book's -- the market as a
                whole moved, not one stale book
  mu_ratio      the book implies a mean over max_mu_ratio (2.5x) the model's
                or under 1/2.5 of it -- more likely a role change or a bad
                player match than a stale book
  model_mismatch  the IMM's own run of the model (its status file) prices
                the market more than 15% apart -- one of the two is broken
  stale_*       the model or the ESPN roster read is old
  too_late      inside stop_before_kickoff_min (15) of kickoff (ESPN's
                scoreboard); too_early past max_hours_before (72)
  cooldown      this bot traded the market in the last cooldown_secs (900)

RECENT FORM, A SECOND OPINION. The model's mean is a long EWMA (8-game
half-life, prior seasons at half weight), slow to see a role that grew:
on 10/4 Matthew Golden's yards ladder sat at 96 yds against a model mean
of 51 -- and his last four games were 84, 95, 58, 100. So the band a book
must clear is the WIDER of the model's and one built the same way (x0.7 /
x1.5) on the mean of the player's last recent_games (4) games, and the
edge is measured against the less favourable of the two fairs. A book
outside both is stale or wrong under either reading of the player.

THE PRICE. An order's limit is the WORST price it accepts (it sweeps every
level at or better than it). A sell's limit is the lowest price on the
market's tick that is (a) band_margin_cents (1) over the band top, (b)
strictly over the IMM's own bid cap (band top + 1c, on the tick) -- so the
sweep never reaches the IMM's bids: the two bots never trade with each
other, and the order also carries self-trade prevention taker_at_cross --
and (c) worth min_net_cents (2) per contract after Kalshi's taker fee
(0.07 x P x (1 - P)) AND min_ror (3%) of the money it puts at risk
(100 - P cents for a sell, P for a buy). A buy mirrors it under the band
bottom.

SIZE. The external liquidity inside the limit (our own resting orders
netted out), capped by max_contracts_order (300) and by the money at risk:
max_risk_market ($40), max_risk_player ($80), max_risk_total ($250) over
this bot's open book, and the account's free cash on the market's shard
less cash_floor ($500, left for the IMM). Selling YES at 3c puts 97c a
contract at risk, so the caps bind long before the book does on cheap
contracts. At most max_orders_per_scan (3) orders a scan, max_orders_per_day
(40) a day.

ONE ACCOUNT, TWO BOTS. On the IMM's account (subaccount 0, the default) the
IMM would read this bot's positions as manual trading and stand off the
whole event (MANUAL_STANDOFF_CONTRACTS) -- incentive_mm.fetch_positions
nets out the book this bot writes (run-logs/nfl-snipe/snipe_book.json).
Better: a numbered Kalshi subaccount (--subaccount N) has its own cash and
positions, which the IMM's reads (primary by default) never see. Creating
it and moving cash into it are the account owner's steps (--setup prints
them); this bot never moves money.

DRY RUN BY DEFAULT: it reads everything, sizes everything, logs "[DRY]
would SELL ..." and books a PAPER fill (snipe_book_paper.json) so its caps
and cooldowns behave as they would live. --live sends orders.

    python nfl_snipe_bot.py                    # dry run, every 20 s
    python nfl_snipe_bot.py --once             # one dry-run scan, then exit
    python nfl_snipe_bot.py --live             # trades (subaccount 0)
    python nfl_snipe_bot.py --live --subaccount 1
    python nfl_snipe_bot.py --report           # the live and paper books
    python nfl_snipe_bot.py --pnl paper        # P&L of the trades vs settlement
    python nfl_snipe_bot.py --until 2026-10-05T00:06Z   # stop at that time
    python nfl_snipe_bot.py --setup            # subaccount setup steps
Every threshold is also an env var: SNIPE_<FIELD> (SNIPE_MAX_RISK_TOTAL=500).
Halt: create run-logs/nfl-snipe/HALT (no orders while it exists).
Logs: run-logs/nfl-snipe/ -- snipe_<date>.log, decisions_ / orders_ /
fills_<date>.jsonl, snipe_status.json.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sys
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import nfl_prop_fair as nf
from nfl_prop_snipe import implied_mu, taker_fee_cents

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("SNIPE_LOG_DIR",
                         os.path.join(HERE, "run-logs", "nfl-snipe"))
BOOK_FILE = "snipe_book.json"            # live, subaccount 0: the IMM nets it out
PAPER_BOOK_FILE = "snipe_book_paper.json"
STATUS_FILE = "snipe_status.json"
WATCH_FILE = "watch_status.json"         # ESPN designations seen, across restarts
# the IMM's refresher writes the same model's snapshot: its team news (a
# designation CLEARED is only known to a watch that saw it set) and its fair
# for a cross-check
IMM_STATUS_FILE = os.environ.get(
    "SNIPE_IMM_STATUS_FILE",
    os.path.join(HERE, "run-logs", "incentive-mm", "nfl_prop_fair.json"))
IMM_FAIR_MAX_AGE_SECS = 900
HALT_FILE = "HALT"
CLIENT_PREFIX = "snp"
ESPN_SCOREBOARD = ("https://site.web.api.espn.com/apis/site/v2/sports/"
                   "football/nfl/scoreboard?dates={day}")
# Kalshi's team codes -> ESPN's scoreboard abbreviations (the rest agree)
KALSHI_TO_ESPN = {"JAC": "JAX", "WAS": "WSH", "LA": "LAR"}
# a played market leaves the book this long after kickoff (settled by then
# or about to be; the risk is no longer this bot's to size against)
PRUNE_AFTER_KICKOFF_SECS = 8 * 3600
NOTE_EVERY_SECS = 900                    # a skipped signal's log line, at most


# --------------------------------------------------------------------- config

@dataclass
class Config:
    live: bool = False
    subaccount: int = 0
    scan_secs: float = 20.0
    sides: str = "both"                  # sell | buy | both
    band_margin_cents: float = 1.0       # past the band edge
    imm_cap_tol_cents: float = 1.0       # the IMM bids up to band top + this
    min_net_cents: float = 2.0           # per contract vs the fair, after fee
    min_ror: float = 0.03                # net / money at risk per contract
    max_mu_ratio: float = 2.5            # book-implied mean vs the model's
    max_risk_market: float = 40.0        # dollars at risk, this bot's book
    max_risk_player: float = 80.0
    max_risk_total: float = 250.0
    max_contracts_order: int = 300
    min_contracts_order: int = 5
    cash_floor: float = 500.0            # free cash left on the shard
    stop_before_kickoff_min: float = 15.0
    max_hours_before: float = 72.0
    news_skip_hours: float = 12.0
    roster_ttl_min: float = 45.0
    model_ttl_min: float = 10.0
    sibling_tol: float = 0.15
    model_mismatch_tol: float = 0.15     # vs the IMM's fair for the market
    recent_games: int = 4                # the recent-form band's window
    cooldown_secs: float = 900.0
    max_orders_per_scan: int = 3
    max_orders_per_day: int = 40

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "Config":
        env = os.environ if environ is None else environ
        cfg = cls()
        for f in fields(cls):
            raw = env.get("SNIPE_" + f.name.upper())
            if raw is None or str(raw).strip() == "":
                continue
            try:
                if f.type in ("bool", bool):
                    val: Any = str(raw).strip().lower() in ("1", "true", "yes")
                elif f.type in ("int", int):
                    val = int(float(raw))
                elif f.type in ("float", float):
                    val = float(raw)
                else:
                    val = str(raw).strip().lower()
            except (TypeError, ValueError):
                continue
            setattr(cfg, f.name, val)
        return cfg


# ------------------------------------------------------------------ logging

def _utc(ts: Optional[float] = None) -> datetime:
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc)


class Log:
    def __init__(self, log_dir: str, echo: bool = True):
        self.dir = log_dir
        self.echo = echo
        os.makedirs(log_dir, exist_ok=True)

    def __call__(self, msg: str) -> None:
        now = _utc()
        line = f"{now.strftime('%Y-%m-%d %H:%M:%SZ')} [SNIPE] {msg}"
        if self.echo:
            print(line, flush=True)
        try:
            with open(os.path.join(self.dir, f"snipe_{now.strftime('%Y-%m-%d')}.log"),
                      "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def row(self, name: str, row: dict) -> None:
        now = _utc()
        row = {"ts": now.isoformat(), **row}
        try:
            with open(os.path.join(self.dir, f"{name}_{now.strftime('%Y-%m-%d')}.jsonl"),
                      "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass


def write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, path)


# --------------------------------------------------------------- price math

def fee_cents(p: float) -> float:
    """Kalshi's taker fee per contract in cents at YES price p (cents)."""
    return taker_fee_cents(p)


def tick_at(m: dict, p: float) -> float:
    """The market's price step in cents at p (its price_ranges; 1c when the
    market carries none)."""
    best: Optional[float] = None
    for r in m.get("price_ranges") or []:
        try:
            lo, hi, st = (float(r[k]) * 100.0 for k in ("start", "end", "step"))
        except (KeyError, TypeError, ValueError):
            continue
        if st > 0 and lo - 1e-9 <= p <= hi + 1e-9:
            best = st if best is None else min(best, st)
    return round(best, 4) if best is not None else 1.0


def ceil_tick(p: float, tick: float) -> float:
    return round(math.ceil(p / tick - 1e-7) * tick, 4)


def floor_tick(p: float, tick: float) -> float:
    return round(math.floor(p / tick + 1e-7) * tick, 4)


def _bisect(ok: Callable[[float], bool], a: float, b: float,
            want_low: bool) -> float:
    """ok is monotone on [a, b]: the smallest passing point (want_low, ok
    false -> true) or the largest (ok true -> false)."""
    for _ in range(60):
        mid = (a + b) / 2.0
        if ok(mid) == want_low:
            b = mid
        else:
            a = mid
    return b if want_low else a


def sell_limit(fair: float, hi: float, tick: float, cfg: Config) -> Optional[float]:
    """The lowest YES price (cents, on the tick) this bot sells at: past the
    band top by band_margin_cents, strictly over the IMM's bid cap, and
    worth min_net_cents and min_ror of the 100 - P at risk after the fee.
    None when no price under 100 qualifies."""
    imm_cap = floor_tick(hi + cfg.imm_cap_tol_cents, tick)
    a = max(hi + cfg.band_margin_cents, imm_cap + tick, tick)
    b = 100.0 - tick

    def ok(x: float) -> bool:
        net = x - fair - fee_cents(x)
        return (net >= cfg.min_net_cents - 1e-9
                and net >= cfg.min_ror * (100.0 - x) - 1e-9)

    if a > b or not ok(b):
        return None
    x = a if ok(a) else _bisect(ok, a, b, want_low=True)
    lim = ceil_tick(x, tick)
    return lim if lim <= b + 1e-9 else None


def buy_limit(fair: float, lo: float, tick: float, cfg: Config) -> Optional[float]:
    """The highest YES price (cents, on the tick) this bot buys at: under
    the band bottom by band_margin_cents, strictly under the IMM's ask floor
    (band bottom - 1c), and worth min_net_cents and min_ror of the P at
    risk after the fee. None when no price over 0 qualifies."""
    imm_floor = ceil_tick(lo - cfg.imm_cap_tol_cents, tick)
    b = min(lo - cfg.band_margin_cents, imm_floor - tick, 100.0 - tick)
    a = tick

    def ok(x: float) -> bool:
        net = fair - x - fee_cents(x)
        return (net >= cfg.min_net_cents - 1e-9
                and net >= cfg.min_ror * x - 1e-9)

    if b < a or not ok(a):
        return None
    x = b if ok(b) else _bisect(ok, a, b, want_low=False)
    lim = floor_tick(x, tick)
    return lim if lim >= a - 1e-9 else None


# ------------------------------------------------------------------- books

def _px(m: dict, key: str) -> Optional[float]:
    """A market object's dollar price field in cents, None off (0, 1)."""
    try:
        v = float(m.get(key))
    except (TypeError, ValueError):
        return None
    return round(v * 100.0, 4) if 0.0 < v < 1.0 else None


def book_levels(ob: dict) -> Tuple[List[Tuple[float, float]],
                                   List[Tuple[float, float]]]:
    """(YES bids best first, YES asks best first) in exact cents from a
    Kalshi order-book read; a YES ask is a NO bid at 100 - price."""
    o = ob.get("orderbook_fp") or ob.get("orderbook") or ob or {}

    def rows(dollars_key: str, cents_key: str) -> List[Tuple[float, float]]:
        out = []
        for px, q in o.get(dollars_key) or []:
            out.append((float(px) * 100.0, float(q)))
        if not out:
            for px, q in o.get(cents_key) or []:
                out.append((float(px), float(q)))
        return out

    bids = sorted(((round(p, 4), q) for p, q in rows("yes_dollars", "yes")
                   if q > 0), reverse=True)
    asks = sorted((round(100.0 - p, 4), q) for p, q in rows("no_dollars", "no")
                  if q > 0)
    return bids, asks


def net_out(levels: List[Tuple[float, float]],
            own: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """levels minus our own resting orders at the same prices."""
    mine: Dict[float, float] = {}
    for p, q in own:
        k = round(p, 4)
        mine[k] = mine.get(k, 0.0) + q
    out = []
    for p, q in levels:
        left = q - mine.get(round(p, 4), 0.0)
        if left > 1e-9:
            out.append((p, left))
    return out


def sweep(levels: List[Tuple[float, float]], limit: float, side: str,
          n: float) -> Tuple[float, float]:
    """(contracts, average price in cents) an order for n at `limit` takes
    from `levels` (best first): bids >= limit for a sell, asks <= limit for
    a buy."""
    got = cost = 0.0
    for p, q in levels:
        if (side == "sell" and p < limit - 1e-9) or (side == "buy" and p > limit + 1e-9):
            break
        take = min(q, n - got)
        got += take
        cost += take * p
        if got >= n - 1e-9:
            break
    return got, (cost / got if got > 0 else 0.0)


def risk_per_contract(side: str, price: float) -> float:
    """Dollars a contract can lose: 100 - P for a YES sell, P for a buy."""
    return (100.0 - price) / 100.0 if side == "sell" else price / 100.0


# ---------------------------------------------------------- the bot's book

class SnipeBook:
    """This bot's open positions from its own fills: ticker -> signed YES
    contracts and net YES premium paid (dollars). Persisted; the IMM nets
    `positions` out of the account's (live book only)."""

    def __init__(self, path: str, subaccount: int = 0):
        self.path = path
        self.subaccount = int(subaccount)
        self.d: Dict[str, Any] = {"positions": {}, "cost": {}, "fees": {},
                                  "player": {}, "kickoff": {}, "last_trade": {},
                                  "fills": [], "closed": [],
                                  "day": {"date": "", "orders": 0}}
        try:
            with open(path, encoding="utf-8") as f:
                js = json.load(f)
            for k, v in js.items():
                if k in self.d:
                    self.d[k] = v
        except (OSError, ValueError):
            pass

    def pos(self, t: str) -> float:
        return float(self.d["positions"].get(t, 0.0))

    def risk(self, t: str) -> float:
        """Dollars this market can still lose: the premium paid on a long,
        (contracts - premium received) on a short."""
        pos, cost = self.pos(t), float(self.d["cost"].get(t, 0.0))
        if pos > 1e-9:
            return max(0.0, cost)
        if pos < -1e-9:
            return max(0.0, -pos + cost)
        return 0.0

    def player_risk(self, key: str) -> float:
        return sum(self.risk(t) for t, k in self.d["player"].items() if k == key)

    def total_risk(self) -> float:
        return sum(self.risk(t) for t in list(self.d["positions"]))

    def last_trade(self, t: str) -> float:
        return float(self.d["last_trade"].get(t, 0.0))

    def orders_today(self, now_ts: float) -> int:
        day = _utc(now_ts).strftime("%Y-%m-%d")
        return int(self.d["day"].get("orders", 0)) if self.d["day"].get("date") == day else 0

    def note_order(self, now_ts: float) -> None:
        day = _utc(now_ts).strftime("%Y-%m-%d")
        if self.d["day"].get("date") != day:
            self.d["day"] = {"date": day, "orders": 0}
        self.d["day"]["orders"] = int(self.d["day"].get("orders", 0)) + 1

    def apply_fill(self, t: str, side: str, n: float, avg_cents: float,
                   fee_dollars: float, player: str, kickoff: Optional[float],
                   now_ts: float, order_id: str = "", paper: bool = False) -> None:
        sign = 1.0 if side == "buy" else -1.0
        self.d["positions"][t] = round(self.pos(t) + sign * n, 4)
        self.d["cost"][t] = round(float(self.d["cost"].get(t, 0.0))
                                  + sign * n * avg_cents / 100.0, 6)
        self.d["fees"][t] = round(float(self.d["fees"].get(t, 0.0)) + fee_dollars, 6)
        self.d["player"][t] = player
        if kickoff is not None:
            self.d["kickoff"][t] = kickoff
        self.d["last_trade"][t] = now_ts
        self.d["fills"].append({"ts": now_ts, "ticker": t, "side": side, "n": n,
                                "avg_cents": round(avg_cents, 4),
                                "fee": round(fee_dollars, 4),
                                "order_id": order_id, "paper": paper})
        self.d["fills"] = self.d["fills"][-500:]
        if abs(self.d["positions"][t]) < 1e-9:
            self.d["positions"].pop(t, None)

    def prune(self, now_ts: float) -> List[str]:
        """Markets whose game started PRUNE_AFTER_KICKOFF_SECS ago leave the
        open book (kept in `closed`, last 500)."""
        gone = []
        for t, ko in list(self.d["kickoff"].items()):
            if ko is not None and now_ts - float(ko) > PRUNE_AFTER_KICKOFF_SECS:
                gone.append(t)
        for t in gone:
            if t in self.d["positions"] or t in self.d["cost"]:
                self.d["closed"].append({
                    "ticker": t, "pos": self.d["positions"].get(t, 0.0),
                    "cost": self.d["cost"].get(t, 0.0),
                    "fees": self.d["fees"].get(t, 0.0),
                    "player": self.d["player"].get(t), "kickoff": self.d["kickoff"][t]})
            for k in ("positions", "cost", "fees", "player", "kickoff", "last_trade"):
                self.d[k].pop(t, None)
        self.d["closed"] = self.d["closed"][-500:]
        return gone

    def save(self) -> None:
        self.d["ts"] = time.time()
        # the IMM nets only a subaccount-0 book out of the account's positions
        self.d["subaccount"] = self.subaccount
        write_json(self.path, self.d)


# --------------------------------------------------------------- kickoffs

class KickoffResolver:
    """Kickoff (epoch seconds) of the game a prop's event ticker names, from
    ESPN's NFL scoreboard: the ticker's date and the next day, the game
    whose two teams match. Cached 6h (a miss: 30 min)."""

    POS_TTL = 6 * 3600
    NEG_TTL = 1800

    def __init__(self, get_json: Optional[Callable[[str], dict]] = None):
        self._get = get_json or self._http
        self.cache: Dict[str, Tuple[float, Optional[float]]] = {}

    @staticmethod
    def _http(url: str) -> dict:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        r.raise_for_status()
        return r.json()

    def kickoff(self, event_ticker: str, now_ts: Optional[float] = None) -> Optional[float]:
        now_ts = time.time() if now_ts is None else now_ts
        parts = event_ticker.split("-")
        seg = parts[1] if len(parts) > 1 else ""
        hit = self.cache.get(seg)
        if hit and now_ts < hit[0]:
            return hit[1]
        ko = None
        try:
            ko = self._resolve(event_ticker, seg)
        except Exception:                                # noqa: BLE001
            ko = None
        self.cache[seg] = (now_ts + (self.POS_TTL if ko else self.NEG_TTL), ko)
        return ko

    def _resolve(self, event_ticker: str, seg: str) -> Optional[float]:
        teams = nf.split_event_teams(event_ticker)
        if not teams or len(seg) < 7:
            return None
        day = datetime.strptime(seg[:7].title(), "%y%b%d")
        want = {KALSHI_TO_ESPN.get(t, t) for t in teams}
        for k in (0, 1):
            d = (day + timedelta(days=k)).strftime("%Y%m%d")
            js = self._get(ESPN_SCOREBOARD.format(day=d)) or {}
            for ev in js.get("events") or []:
                st = (((ev.get("status") or {}).get("type") or {}).get("name") or "")
                if st in ("STATUS_POSTPONED", "STATUS_CANCELED", "STATUS_CANCELLED"):
                    continue
                comps = ((ev.get("competitions") or [{}])[0].get("competitors") or [])
                abbrs = {str((c.get("team") or {}).get("abbreviation") or "").upper()
                         for c in comps}
                if abbrs == want:
                    return nf._espn_ts(ev.get("date"))
        return None


# --------------------------------------------------------------- exchange

class Exchange:
    """Kalshi I/O. Reads go through kalshi_reads (the IMM's signed client;
    books public first); orders through the same signed client, IOC only.
    `subaccount` > 0 scopes this bot's balance and orders to a numbered
    Kalshi subaccount."""

    def __init__(self, subaccount: int = 0):
        self.subaccount = int(subaccount)

    @staticmethod
    def _get(path: str, params: Optional[dict] = None, prefer: str = "signed") -> Any:
        from kalshi_reads import kalshi_get
        return kalshi_get(path, params, prefer=prefer)

    def open_markets(self) -> List[dict]:
        from kalshi_reads import kalshi_get_all
        out: List[dict] = []
        for s in nf.SERIES:
            out += kalshi_get_all("/markets", {"series_ticker": s, "status": "open",
                                               "limit": 1000}, items_key="markets")
        return out

    def orderbook(self, t: str) -> dict:
        return self._get(f"/markets/{t}/orderbook", None, prefer="public")

    def own_resting(self, t: str) -> List[Tuple[str, float, float]]:
        """The account owner's resting orders on t (the IMM's quotes):
        (book side, YES price cents, remaining contracts)."""
        js = self._get("/portfolio/orders", {"ticker": t, "status": "resting",
                                             "limit": 1000})
        out = []
        for o in js.get("orders") or []:
            try:
                out.append((str(o.get("book_side") or ""),
                            round(float(o["yes_price_dollars"]) * 100.0, 4),
                            float(o.get("remaining_count_fp") or 0.0)))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def free_cash(self, shard: int = 0) -> Optional[float]:
        """Dollars on the shard for this bot's subaccount (the balance does
        not net out resting orders: measured 10/4, $339 of bids cancelled
        and the balance did not move)."""
        params = {"subaccount": self.subaccount} if self.subaccount else None
        js = self._get("/portfolio/balance", params)
        for b in js.get("balance_breakdown") or []:
            if int(b.get("exchange_index", -1)) == int(shard):
                return float(b.get("balance") or 0.0)
        try:
            return float(js.get("balance_dollars"))
        except (TypeError, ValueError):
            return float(js.get("balance") or 0.0) / 100.0

    def place_ioc(self, ticker: str, side: str, count: int, limit_cents: float,
                  client_order_id: str) -> dict:
        """An immediate-or-cancel order: side 'sell' = sell YES (the book's
        ask side), 'buy' = buy YES. Never rests; never trades against the
        account's own orders (taker_at_cross cancels it there instead)."""
        from kalshi_reads import signed_client
        c = signed_client()
        body = {"ticker": ticker,
                "side": "ask" if side == "sell" else "bid",
                "count": f"{float(count):.2f}",
                "price": f"{limit_cents / 100.0:.4f}",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": client_order_id}
        if self.subaccount:
            body["subaccount"] = self.subaccount
        return c.post(path=c.events_orders_url, body=json.dumps(body))


def _signed_get_json(path: str, params: dict) -> dict:
    from kalshi_reads import kalshi_get
    return kalshi_get(path, params)


# ------------------------------------------------------------ the decision

@dataclass
class Signal:
    ticker: str
    side: str                 # 'sell' (YES into a bid) | 'buy' (YES from an ask)
    top: float                # the book's best price on that side, cents
    limit: float              # the worst price accepted, cents
    tick: float
    fair: float               # the side's fair: the less favourable view
    lo: float                 # the band the book cleared (the wider one)
    hi: float
    net: float                # cents a contract at the top, after the fee
    ror: float                # net / cents at risk a contract, at the top
    player: str = ""
    player_key: str = ""
    stat: str = ""
    kind: str = ""
    mu: float = 0.0
    implied_mu: Optional[float] = None
    kickoff: Optional[float] = None
    model_fair: Optional[float] = None    # nfl_prop_fair's, cents
    recent_mean: Optional[float] = None   # last recent_games games
    reason: str = ""          # why it was skipped ('' = tradeable)


def recent_mean(games: List[dict], stat: str, k: int) -> Optional[float]:
    """The mean of the stat over the player's last k games (negative yards
    as 0, like the model); None under 3 games."""
    sm = nf.STAT_MODEL[stat]
    clip = not sm["discrete"] and stat != "ffpts"
    vals = []
    for g in games[-k:]:
        v = g.get(sm["col"])
        if v is None or v == "":
            continue
        v = float(v)
        vals.append(max(0.0, v) if clip else v)
    return sum(vals) / len(vals) if len(vals) >= 3 else None


def recent_band(m: dict, e: dict, mean: float) -> Tuple[float, float, float]:
    """(fair, lo, hi) in cents with the recent mean in the model's place:
    the same payout, the same x BAND_LO / x BAND_HI band, the entry's
    teammate multiplier applied."""
    spec = nf.payout_spec(m, e["stat"], e["kind"])
    mu = mean * float(e.get("team_mult") or 1.0)
    f, lo, hi = (nf.expected_payout(spec, x) * 100.0
                 for x in (mu, mu * nf.BAND_LO, mu * nf.BAND_HI))
    return f, lo, hi


def find_signal(t: str, m: dict, e: Optional[dict], cfg: Config,
                recent: Optional[Tuple[float, float, float]] = None
                ) -> Optional[Signal]:
    """The book outside the band far enough to take, or None. `recent`:
    (fair, lo, hi) cents on the recent-form mean -- a sell then clears the
    higher band top against the higher fair, a buy the lower bottom
    against the lower fair."""
    if not e or e.get("err") or e.get("fair") is None:
        return None
    f, lo, hi = e["fair"] * 100.0, e["fair_lo"] * 100.0, e["fair_hi"] * 100.0
    fs, his, fb, lob = f, hi, f, lo
    if recent is not None:
        rf, rlo, rhi = recent
        fs, his, fb, lob = max(f, rf), max(hi, rhi), min(f, rf), min(lo, rlo)
    who = dict(player=e.get("player") or "", stat=e.get("stat") or "",
               kind=e.get("kind") or "", mu=float(e.get("mu") or 0.0),
               player_key=f"{e.get('pid') or e.get('player')}|{e.get('team')}")
    bid, ask = _px(m, "yes_bid_dollars"), _px(m, "yes_ask_dollars")
    if cfg.sides in ("both", "sell") and bid is not None:
        tk = tick_at(m, bid)
        lim = sell_limit(fs, his, tk, cfg)
        if lim is not None and bid >= lim - 1e-9:
            net = bid - fs - fee_cents(bid)
            return Signal(t, "sell", bid, lim, tk, fs, lo, his, net,
                          net / max(100.0 - bid, 1e-9), model_fair=f, **who)
    if cfg.sides in ("both", "buy") and ask is not None:
        tk = tick_at(m, ask)
        lim = buy_limit(fb, lob, tk, cfg)
        if lim is not None and ask <= lim + 1e-9:
            net = fb - ask - fee_cents(ask)
            return Signal(t, "buy", ask, lim, tk, fb, lob, hi, net,
                          net / max(ask, 1e-9), model_fair=f, **who)
    return None


def implied_at(m: dict, e: dict, price: float) -> Optional[float]:
    """The mean the model would need to price this contract at `price`."""
    try:
        spec = nf.payout_spec(m, e["stat"], e["kind"])
        return implied_mu(spec, price, max(float(e.get("mu") or 1.0), 1.0) * 3.0)
    except Exception:                                    # noqa: BLE001
        return None


def sibling_means(t: str, e: dict, entries: Dict[str, dict],
                  markets: Dict[str, dict],
                  by_player: Dict[Tuple[Any, str], List[str]]) -> Dict[str, float]:
    """The player's other contracts on the same stat, each book's implied
    mean at its mid (books with a usable spread only)."""
    out: Dict[str, float] = {}
    for t2 in by_player.get((e.get("pid"), e.get("stat")), []):
        if t2 == t or t2 not in markets:
            continue
        e2, m2 = entries.get(t2) or {}, markets[t2]
        b, a = _px(m2, "yes_bid_dollars"), _px(m2, "yes_ask_dollars")
        if b is None or a is None or a <= b:
            continue
        mid = (a + b) / 2.0
        if a - b > max(2.0, 0.5 * mid):
            continue                     # a wide book says nothing
        imu = implied_at(m2, e2, mid)
        if imu is not None:
            out[t2] = imu
    return out


def skip_reason(sig: Signal, e: dict, snap_ts: float, now_ts: float,
                siblings: Dict[str, float], book: SnipeBook, cfg: Config,
                imm_fair: Optional[float] = None) -> str:
    """'' when the signal may be taken, else why not (see the module doc).
    imm_fair: the IMM's fair for the market (cents), when its snapshot is
    fresh -- the two runs of the model must agree."""
    if now_ts - snap_ts > cfg.model_ttl_min * 60:
        return "stale_model"
    if sig.kickoff is None:
        return "kickoff_unknown"
    if now_ts >= sig.kickoff - cfg.stop_before_kickoff_min * 60:
        return "too_late"
    if sig.kickoff - now_ts > cfg.max_hours_before * 3600:
        return "too_early"
    rts = e.get("roster_ts")
    if rts is None or now_ts - float(rts) > cfg.roster_ttl_min * 60:
        return "stale_roster"
    if e.get("injury"):
        return "injury"
    na = e.get("news_at")
    if na is not None and now_ts - float(na) < cfg.news_skip_hours * 3600:
        return "team_news"
    mf = sig.model_fair if sig.model_fair is not None else sig.fair
    if imm_fair is not None and abs(imm_fair - mf) > max(
            cfg.model_mismatch_tol * max(imm_fair, mf), 0.2):
        return "model_mismatch"
    imu, mu = sig.implied_mu, sig.mu
    if imu is None or mu <= 0:
        return "no_implied_mean"
    if imu > cfg.max_mu_ratio * mu or imu < mu / cfg.max_mu_ratio:
        return "mu_ratio"
    if any(abs(s - imu) <= cfg.sibling_tol * imu for s in siblings.values()):
        return "sibling"
    if now_ts - book.last_trade(sig.ticker) < cfg.cooldown_secs:
        return "cooldown"
    return ""


def espn_team(e: dict) -> Optional[str]:
    """The entry's team as ESPN's lowercase code (the snapshot's team keys)."""
    team = e.get("team")
    if not team:
        return None
    nt = nf.KALSHI_TO_NFLV.get(team, team)
    return nf.NFLV_TO_ESPN.get(nt, nt.lower())


def imm_view(path: str, now_ts: float) -> Tuple[Dict[str, float], Dict[str, float]]:
    """From the IMM's model snapshot: (ESPN team -> latest news time, for
    any age of file -- a news stamp stays true; ticker -> fair in cents,
    only while the file is IMM_FAIR_MAX_AGE_SECS fresh)."""
    try:
        with open(path, encoding="utf-8") as f:
            js = json.load(f)
    except (OSError, ValueError):
        return {}, {}
    news = {k: float(v["news_at"]) for k, v in (js.get("teams") or {}).items()
            if isinstance(v, dict) and v.get("news_at") is not None}
    fairs: Dict[str, float] = {}
    if now_ts - float(js.get("ts") or 0.0) <= IMM_FAIR_MAX_AGE_SECS:
        for t, e in (js.get("markets") or {}).items():
            if isinstance(e, dict) and e.get("fair") is not None and not e.get("err"):
                fairs[t] = float(e["fair"]) * 100.0
    return news, fairs


def size_order(sig: Signal, limit: float, available: float, book: SnipeBook,
               cash: Optional[float], cfg: Config) -> Tuple[int, str]:
    """(contracts, '' or the binding reason when under min_contracts_order)."""
    per = risk_per_contract(sig.side, limit) + fee_cents(limit) / 100.0
    rooms = {"market_cap": cfg.max_risk_market - book.risk(sig.ticker),
             "player_cap": cfg.max_risk_player - book.player_risk(sig.player_key),
             "total_cap": cfg.max_risk_total - book.total_risk()}
    if cash is not None:
        rooms["cash"] = cash - cfg.cash_floor
    by_risk = min(rooms.values())
    n = int(min(available, cfg.max_contracts_order,
                math.floor(max(by_risk, 0.0) / max(per, 1e-9) + 1e-9)))
    if n >= cfg.min_contracts_order:
        return n, ""
    if available < cfg.min_contracts_order:
        return 0, "thin"
    return 0, min(rooms, key=rooms.get)


# ------------------------------------------------------------------ the bot

class Sniper:
    def __init__(self, cfg: Config, exchange: Optional[Exchange] = None,
                 watch: Optional[nf.NflPropWatch] = None,
                 kick: Optional[KickoffResolver] = None,
                 log_dir: str = LOG_DIR, log: Optional[Log] = None):
        self.cfg = cfg
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)
        self.log = log or Log(log_dir)
        self.ex = exchange or Exchange(cfg.subaccount)
        self.watch = watch or nf.NflPropWatch(get_json=_signed_get_json,
                                              cache_dir=_seed_cache(log_dir))
        self.kick = kick or KickoffResolver()
        self.book = SnipeBook(os.path.join(log_dir, book_file(cfg)), cfg.subaccount)
        self.noted: Dict[Tuple[str, str], float] = {}
        self.scans = 0
        self._restore_watch()

    # -- one scan
    def scan(self, now_ts: Optional[float] = None) -> dict:
        now_ts = time.time() if now_ts is None else now_ts
        cfg = self.cfg
        out: Dict[str, Any] = {"ts": now_ts, "live": cfg.live, "markets": 0,
                               "signals": 0, "skips": Counter(), "taken": [],
                               "errors": []}
        self.scans += 1
        if os.path.exists(os.path.join(self.log_dir, HALT_FILE)):
            out["halted"] = True
            return self._finish(out)
        snap = self.watch.refresh(now_ts)
        entries = snap.get("markets") or {}
        out["model_errors"] = list(snap.get("errors") or [])[:5]
        try:
            markets = {m["ticker"]: m for m in self.ex.open_markets()
                       if str(m.get("ticker", "")).split("-", 1)[0] in nf.SERIES}
        except Exception as ex:                          # noqa: BLE001
            out["errors"].append(f"markets: {type(ex).__name__}: {str(ex)[:120]}")
            return self._finish(out)
        out["markets"] = len(markets)
        for t in self.book.prune(now_ts):
            self.log(f"{t}: left the open book (game played)")
        by_player: Dict[Tuple[Any, str], List[str]] = {}
        for t, e in entries.items():
            if e.get("pid") is not None:
                by_player.setdefault((e["pid"], e.get("stat")), []).append(t)
        snap_ts = float(snap.get("ts") or 0.0)
        imm_news, imm_fairs = imm_view(IMM_STATUS_FILE, now_ts)
        takes: List[Signal] = []
        index = getattr(self.watch, "index", None)
        for t, m in markets.items():
            e = entries.get(t)
            if find_signal(t, m, e, cfg) is None:
                continue                 # inside the model's band: no look
            rmean = None
            if index is not None and e.get("pid") in index.games:
                rmean = recent_mean(index.games[e["pid"]], e["stat"], cfg.recent_games)
            sig = find_signal(t, m, e, cfg, recent=(
                recent_band(m, e, rmean) if rmean is not None else None))
            if sig is None:
                out["skips"]["recent_form"] += 1
                continue
            sig.recent_mean = rmean
            out["signals"] += 1
            # the later of this watch's and the IMM's news on the team
            et = espn_team(e)
            if et in imm_news and (e.get("news_at") is None
                                   or imm_news[et] > float(e["news_at"])):
                e = dict(e, news_at=imm_news[et])
            sig.kickoff = self.kick.kickoff(m.get("event_ticker") or t.rsplit("-", 1)[0],
                                            now_ts)
            sig.implied_mu = implied_at(m, e, sig.top)
            sibs = sibling_means(t, e, entries, markets, by_player)
            sig.reason = skip_reason(sig, e, snap_ts, now_ts, sibs, self.book, cfg,
                                     imm_fair=imm_fairs.get(t))
            if sig.reason:
                out["skips"][sig.reason] += 1
                self._note(sig, now_ts, siblings=sibs)
                continue
            takes.append(sig)
        takes.sort(key=lambda s: -s.ror)
        cash: Dict[int, Optional[float]] = {}
        for sig in takes[:cfg.max_orders_per_scan]:
            if self.book.orders_today(now_ts) >= cfg.max_orders_per_day:
                out["skips"]["day_cap"] += 1
                sig.reason = "day_cap"
                self._note(sig, now_ts)
                break
            try:
                res = self._take(sig, markets[sig.ticker], cash, now_ts)
            except Exception as ex:                      # noqa: BLE001
                out["errors"].append(f"{sig.ticker}: {type(ex).__name__}: {str(ex)[:160]}")
                self.log(f"! {sig.ticker}: {type(ex).__name__}: {str(ex)[:200]}")
                continue
            if res.get("skip"):
                out["skips"][res["skip"]] += 1
            else:
                out["taken"].append(res)
        return self._finish(out)

    def _take(self, sig: Signal, m: dict, cash: Dict[int, Optional[float]],
              now_ts: float) -> dict:
        cfg = self.cfg
        bids, asks = book_levels(self.ex.orderbook(sig.ticker))
        own = self.ex.own_resting(sig.ticker)
        limit = sig.limit
        if sig.side == "sell":
            mine = [(p, q) for s, p, q in own if s == "bid"]
            levels = net_out(bids, mine)
            # never sweep into our own bid (the IMM's): stop a tick over it
            top_own = max((p for p, _q in mine), default=None)
            if top_own is not None and top_own >= limit - 1e-9:
                limit = ceil_tick(top_own + sig.tick, sig.tick)
        else:
            mine = [(p, q) for s, p, q in own if s == "ask"]
            levels = net_out(asks, mine)
            low_own = min((p for p, _q in mine), default=None)
            if low_own is not None and low_own <= limit + 1e-9:
                limit = floor_tick(low_own - sig.tick, sig.tick)
        avail, _avg = sweep(levels, limit, sig.side, float("inf"))
        shard = int(m.get("exchange_index") or 0)
        if shard not in cash:
            cash[shard] = self.ex.free_cash(shard)
        n, why = size_order(sig, limit, avail, self.book, cash[shard], cfg)
        if n <= 0:
            sig.reason = why
            self._note(sig, now_ts, available=avail, cash=cash[shard], limit=limit)
            return {"skip": why}
        got, avg = sweep(levels, limit, sig.side, n)
        exp_net = (avg - sig.fair if sig.side == "sell" else sig.fair - avg) - fee_cents(avg)
        verb = "SELL" if sig.side == "sell" else "BUY"
        desc = (f"{sig.ticker} ({sig.player}): {verb} {n} YES at "
                f"{'>=' if sig.side == 'sell' else '<='}{limit:g}c "
                f"[book {sig.top:g}c, fair {sig.fair:.2f}c, band "
                f"{sig.lo:.2f}-{sig.hi:.2f}c, mean {sig.mu:.1f} vs book "
                f"{(sig.implied_mu or 0):.1f}, exp +{exp_net:.2f}c/ct, "
                f"risk ${n * risk_per_contract(sig.side, avg):.2f}]")
        row = {"ticker": sig.ticker, "player": sig.player, "side": sig.side,
               "count": n, "limit": limit, "book_top": sig.top, "fair": sig.fair,
               "lo": sig.lo, "hi": sig.hi, "mu": sig.mu,
               "implied_mu": sig.implied_mu, "kickoff": sig.kickoff,
               "available": avail, "expected_avg": avg,
               "expected_net_cents": exp_net, "live": cfg.live,
               "subaccount": cfg.subaccount}
        coid = f"{CLIENT_PREFIX}-{uuid.uuid4().hex[:20]}"
        self.book.note_order(now_ts)
        if not cfg.live:
            # a PAPER fill at the swept levels, so caps and cooldowns act
            self.book.apply_fill(sig.ticker, sig.side, got, avg,
                                 got * fee_cents(avg) / 100.0, sig.player_key,
                                 sig.kickoff, now_ts, paper=True)
            if cash[shard] is not None:
                cash[shard] -= got * (risk_per_contract(sig.side, avg)
                                      + fee_cents(avg) / 100.0)
            self.log(f"[DRY] would {desc}")
            self.log.row("orders", {**row, "dry": True, "filled": got,
                                    "avg_cents": avg})
            return {**row, "filled": got, "avg_cents": avg, "dry": True}
        try:
            resp = self.ex.place_ioc(sig.ticker, sig.side, n, limit, coid)
        except Exception as ex:                          # noqa: BLE001
            body = getattr(ex, "body", "")
            self.log(f"! order refused: {desc} -- {ex} {body}".rstrip())
            self.log.row("orders", {**row, "client_order_id": coid,
                                    "error": str(ex), "error_body": body})
            return {"skip": "refused"}
        filled = float(resp.get("fill_count") or 0.0)
        avg_fill = float(resp.get("average_fill_price") or 0.0) * 100.0
        fee = float(resp.get("average_fee_paid") or 0.0) * filled
        oid = resp.get("order_id") or ""
        if filled > 0:
            self.book.apply_fill(sig.ticker, sig.side, filled, avg_fill, fee,
                                 sig.player_key, sig.kickoff, now_ts, order_id=oid)
            if cash[shard] is not None:
                cash[shard] -= filled * risk_per_contract(sig.side, avg_fill) + fee
            self.log.row("fills", {**row, "order_id": oid, "client_order_id": coid,
                                   "filled": filled, "avg_cents": avg_fill,
                                   "fee": fee})
        self.log(f"{'FILLED' if filled > 0 else 'no fill'} {desc} -> "
                 f"{filled:g} @ {avg_fill:.2f}c, fee ${fee:.2f} ({oid})")
        self.log.row("orders", {**row, "order_id": oid, "client_order_id": coid,
                                "filled": filled, "avg_cents": avg_fill, "fee": fee,
                                "response": resp})
        self.book.save()
        return {**row, "filled": filled, "avg_cents": avg_fill, "fee": fee,
                "order_id": oid}

    def _restore_watch(self) -> None:
        """The ESPN designations this bot's watch saw before a restart: a
        designation CLEARED while it was down is then still news (the
        injury date only stamps designations that are still on)."""
        w = self.watch
        if not hasattr(w, "status_seen"):
            return
        try:
            with open(os.path.join(self.log_dir, WATCH_FILE), encoding="utf-8") as f:
                js = json.load(f)
            for team, key, st in js.get("seen") or []:
                w.status_seen.setdefault((team, key), st)
            for team, key, ts in js.get("changed") or []:
                w.status_changed_at.setdefault((team, key), float(ts))
        except (OSError, ValueError, TypeError):
            pass

    def _save_watch(self) -> None:
        w = self.watch
        if not hasattr(w, "status_seen"):
            return
        write_json(os.path.join(self.log_dir, WATCH_FILE), {
            "seen": [[t, k, s] for (t, k), s in sorted(w.status_seen.items())],
            "changed": [[t, k, ts] for (t, k), ts in sorted(w.status_changed_at.items())]})

    def _note(self, sig: Signal, now_ts: float, **extra: Any) -> None:
        """A skipped signal: one decisions row (and log line) per ticker and
        reason every NOTE_EVERY_SECS."""
        key = (sig.ticker, sig.reason)
        if now_ts - self.noted.get(key, 0.0) < NOTE_EVERY_SECS:
            return
        self.noted[key] = now_ts
        row = {k: v for k, v in asdict(sig).items()}
        row.update({k: v for k, v in extra.items() if k != "siblings"})
        if extra.get("siblings"):
            row["siblings"] = extra["siblings"]
        self.log.row("decisions", row)
        self.log(f"skip {sig.reason}: {sig.ticker} ({sig.player}) "
                 f"{sig.side} at book {sig.top:g}c, fair {sig.fair:.2f}c, "
                 f"band {sig.lo:.2f}-{sig.hi:.2f}c")

    def _finish(self, out: dict) -> dict:
        out["skips"] = dict(out.get("skips") or {})
        book = self.book
        out["book"] = {"open_markets": len(book.d["positions"]),
                       "risk_total": round(book.total_risk(), 2),
                       "orders_today": book.orders_today(out["ts"])}
        status = {"ts": out["ts"], "at": _utc(out["ts"]).isoformat(),
                  "scans": self.scans, "config": asdict(self.cfg), "last": out}
        try:
            book.save()
            self._save_watch()
            write_json(os.path.join(self.log_dir, STATUS_FILE), status)
        except OSError as ex:
            self.log(f"! status write failed: {ex}")
        return out


def book_file(cfg: Config) -> str:
    """The book's file: the paper book, the live book the IMM reads
    (subaccount 0), or a numbered subaccount's own (the IMM never reads it
    -- its positions are not in the primary's)."""
    if not cfg.live:
        return PAPER_BOOK_FILE
    return BOOK_FILE if not cfg.subaccount else f"snipe_book_sub{cfg.subaccount}.json"


def _seed_cache(log_dir: str) -> str:
    """This bot's own nflverse cache (the IMM's refresher writes its own on
    its own clock), seeded once from the IMM's so the first scan does not
    re-download every season."""
    mine = os.path.join(log_dir, "nfl_stats")
    os.makedirs(mine, exist_ok=True)
    src = nf.CACHE_DIR
    try:
        for name in os.listdir(src):
            if name.endswith(".csv") and not os.path.exists(os.path.join(mine, name)):
                shutil.copy2(os.path.join(src, name), os.path.join(mine, name))
    except OSError:
        pass
    return mine


class SingleInstance:
    """An exclusive lock on a file for the life of the process."""

    def __init__(self, path: str):
        self.path = path
        self.fh = None

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


# ---------------------------------------------------------------------- P&L

SETTLED_STATUSES = ("settled", "finalized", "determined")


def settlement_cents(m: dict) -> Optional[float]:
    """A settled market's YES payout in cents (its settlement value; a
    binary's result), None while it is not settled."""
    if str(m.get("status") or "").lower() not in SETTLED_STATUSES:
        return None
    for key, scale in (("settlement_value_dollars", 100.0), ("settlement_value", 1.0)):
        v = m.get(key)
        if v in (None, ""):
            continue
        try:
            return round(float(v) * scale, 4)
        except (TypeError, ValueError):
            continue
    r = str(m.get("result") or "").lower()
    return 100.0 if r == "yes" else 0.0 if r == "no" else None


def mark_cents(m: dict) -> Optional[float]:
    """An unsettled market's mid (one side or the last trade when that is
    all there is)."""
    b, a = _px(m, "yes_bid_dollars"), _px(m, "yes_ask_dollars")
    if b is not None and a is not None:
        return (a + b) / 2.0
    for v in (b, a, _px(m, "last_price_dollars")):
        if v is not None:
            return v
    return None


def trade_pnl(side: str, n: float, avg_cents: float, fee: float,
              value_cents: float) -> float:
    """Dollars a trade makes against a YES value (settlement or mark)."""
    edge = (avg_cents - value_cents) if side == "sell" else (value_cents - avg_cents)
    return n * edge / 100.0 - fee


def load_trades(log_dir: str, mode: str) -> List[dict]:
    """The bot's trades in time order: the dry-run orders rows (`paper`) or
    the fills rows (`live`), across every day's log."""
    import glob
    name = "orders" if mode == "paper" else "fills"
    out = []
    for path in sorted(glob.glob(os.path.join(log_dir, f"{name}_*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if mode == "paper" and not r.get("dry"):
                    continue
                n = float(r.get("filled") or 0.0)
                if n <= 0:
                    continue
                avg = float(r.get("avg_cents") or 0.0)
                fee = (float(r["fee"]) if r.get("fee") is not None
                       else n * fee_cents(avg) / 100.0)
                out.append({"ts": r.get("ts"), "ticker": r["ticker"],
                            "player": r.get("player") or "", "side": r["side"],
                            "n": n, "avg_cents": avg, "fee": fee,
                            "fair": r.get("fair"),
                            "exp_net_cents": r.get("expected_net_cents"),
                            "kickoff": r.get("kickoff")})
    out.sort(key=lambda r: str(r["ts"]))
    return out


def pnl_report(trades: List[dict], get_market: Callable[[str], dict],
               default_cap: float = 250.0) -> dict:
    """Each trade valued at its market's settlement (or, unsettled, at the
    mid), and totals -- all trades and the first ones that fit the default
    $250 risk cap (what the live defaults would have taken)."""
    cache: Dict[str, dict] = {}
    rows, cum = [], 0.0
    for tr in trades:
        t = tr["ticker"]
        if t not in cache:
            try:
                cache[t] = get_market(t) or {}
            except Exception:                            # noqa: BLE001
                cache[t] = {}
        m = cache[t]
        settle = settlement_cents(m)
        value = settle if settle is not None else mark_cents(m)
        risk = tr["n"] * risk_per_contract(tr["side"], tr["avg_cents"])
        cum += risk
        rows.append({**tr, "risk": risk, "settled": settle is not None,
                     "value_cents": value,
                     "pnl": (trade_pnl(tr["side"], tr["n"], tr["avg_cents"],
                                       tr["fee"], value) if value is not None else None),
                     "expected": (tr["n"] * tr["exp_net_cents"] / 100.0
                                  if tr.get("exp_net_cents") is not None else None),
                     "in_default_cap": cum <= default_cap + 1e-6})

    def total(rs: List[dict]) -> dict:
        done = [r for r in rs if r["settled"] and r["pnl"] is not None]
        open_ = [r for r in rs if not r["settled"]]
        return {"trades": len(rs), "settled": len(done),
                "settled_pnl": round(sum(r["pnl"] for r in done), 2),
                "settled_risk": round(sum(r["risk"] for r in done), 2),
                "settled_expected": round(sum(r["expected"] or 0.0 for r in done), 2),
                "won": sum(1 for r in done if r["pnl"] > 0),
                "open": len(open_),
                "open_marked_pnl": round(sum(r["pnl"] or 0.0 for r in open_), 2)}

    return {"rows": rows, "all": total(rows),
            "default_cap": total([r for r in rows if r["in_default_cap"]])}


def print_pnl(rep: dict, mode: str) -> None:
    print(f"\n{mode} trades: P&L at settlement (open ones at the mid)")
    print(f"{'ticker':56s} {'player':20s} {'side':4s} {'n':>5s} {'@':>7s} "
          f"{'value':>7s} {'P&L $':>8s} {'exp $':>7s}  state")
    for r in rep["rows"]:
        val = f"{r['value_cents']:.2f}" if r["value_cents"] is not None else "-"
        pnl = f"{r['pnl']:+.2f}" if r["pnl"] is not None else "-"
        exp = f"{r['expected']:+.2f}" if r["expected"] is not None else "-"
        state = "settled" if r["settled"] else "OPEN (mid)"
        cap = "" if r["in_default_cap"] else "  past $250"
        print(f"{r['ticker']:56s} {r['player'][:20]:20s} {r['side']:4s} {r['n']:5.0f} "
              f"{r['avg_cents']:7.2f} {val:>7s} {pnl:>8s} {exp:>7s}  {state}{cap}")
    for key, label in (("all", "all trades"), ("default_cap", "inside the $250 default cap")):
        tt = rep[key]
        ror = (tt["settled_pnl"] / tt["settled_risk"]) if tt["settled_risk"] else 0.0
        print(f"\n{label}: {tt['settled']}/{tt['trades']} settled, "
              f"P&L ${tt['settled_pnl']:+.2f} on ${tt['settled_risk']:.2f} at risk "
              f"({ror:+.1%}), {tt['won']} won; expected ${tt['settled_expected']:+.2f}; "
              f"{tt['open']} open, marked ${tt['open_marked_pnl']:+.2f}")


def parse_until(v: Optional[str]) -> Optional[float]:
    """--until: an ISO time (UTC when no zone) or epoch seconds."""
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    v = v.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# --------------------------------------------------------------------- CLI

SETUP_TEXT = """\
Running the sniper in its own Kalshi subaccount (recommended): its cash and
positions stay apart from the IMM's, and the IMM's reads (the primary
subaccount by default) never see them. Both steps are the account owner's --
this bot never creates accounts or moves money.

 1. Create a numbered subaccount: Kalshi's API, POST /portfolio/subaccounts
    (docs.kalshi.com, "Create Subaccount"), or the website if offered.
    Note its number (1-63).
 2. Move cash into it: POST /portfolio/subaccounts/transfer with
      {"client_transfer_id": "<new uuid>", "from_subaccount": 0,
       "to_subaccount": <N>, "amount_cents": <cents>, "exchange_index": 0}
    NFL props trade on exchange shard 0.
 3. Dry run against it, then go live:
      python nfl_snipe_bot.py --once --subaccount <N>
      python nfl_snipe_bot.py --live --subaccount <N>
On the primary account instead (--subaccount 0), the IMM must net this bot's
book out of the account's positions (incentive_mm.fetch_positions reads
run-logs/nfl-snipe/snipe_book.json), or it reads every snipe as a manual
trade and stands off the whole event.
"""


def report(log_dir: str) -> None:
    for name in (BOOK_FILE, PAPER_BOOK_FILE):
        b = SnipeBook(os.path.join(log_dir, name))
        pos = b.d["positions"]
        print(f"\n{name}: {len(pos)} open markets, ${b.total_risk():.2f} at risk, "
              f"{len(b.d['fills'])} fills logged, {len(b.d['closed'])} closed")
        for t in sorted(pos):
            p, c = pos[t], float(b.d["cost"].get(t, 0.0))
            avg = abs(c / p) * 100.0 if p else 0.0
            print(f"  {t:56s} {p:+8.0f} @ {avg:6.2f}c  risk ${b.risk(t):7.2f}  "
                  f"{b.d['player'].get(t, '')}")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="send orders (default: dry run)")
    ap.add_argument("--once", action="store_true", help="one scan, then exit")
    ap.add_argument("--subaccount", type=int, default=None,
                    help="Kalshi subaccount number (default 0 = primary)")
    ap.add_argument("--sides", choices=("sell", "buy", "both"), default=None)
    ap.add_argument("--max-risk-total", type=float, default=None)
    ap.add_argument("--report", action="store_true", help="print the books and exit")
    ap.add_argument("--pnl", choices=("paper", "live"), default=None,
                    help="P&L of the paper / live trades against settlement")
    ap.add_argument("--until", default=None,
                    help="stop scanning at this time (ISO, UTC) or epoch")
    ap.add_argument("--setup", action="store_true", help="subaccount setup steps")
    ap.add_argument("--log-dir", default=LOG_DIR)
    args = ap.parse_args(argv)
    if args.setup:
        print(SETUP_TEXT)
        return 0
    if args.report:
        report(args.log_dir)
        return 0
    if args.pnl:
        from kalshi_reads import kalshi_get

        def get_market(t: str) -> dict:
            return (kalshi_get(f"/markets/{t}", None, prefer="public") or {}).get("market") or {}

        rep = pnl_report(load_trades(args.log_dir, args.pnl), get_market)
        print_pnl(rep, args.pnl)
        write_json(os.path.join(args.log_dir, f"pnl_{args.pnl}.json"),
                   {"at": _utc().isoformat(), **rep})
        return 0
    until = parse_until(args.until)
    cfg = Config.from_env()
    if args.live:
        cfg.live = True
    if args.subaccount is not None:
        cfg.subaccount = args.subaccount
    if args.sides:
        cfg.sides = args.sides
    if args.max_risk_total is not None:
        cfg.max_risk_total = args.max_risk_total
    lock = SingleInstance(os.path.join(
        args.log_dir, "snipe_live.lock" if cfg.live else "snipe_paper.lock"))
    if not lock.acquire():
        print(f"another {'live' if cfg.live else 'dry-run'} sniper holds the lock; exiting")
        return 1
    bot = Sniper(cfg, log_dir=args.log_dir)
    bot.log(f"start: {'LIVE' if cfg.live else 'DRY RUN'}, subaccount {cfg.subaccount}, "
            f"sides {cfg.sides}, margin {cfg.band_margin_cents}c past the band, "
            f"min {cfg.min_net_cents}c and {cfg.min_ror:.0%} of risk a contract, "
            f"risk caps ${cfg.max_risk_market:g} market / ${cfg.max_risk_player:g} "
            f"player / ${cfg.max_risk_total:g} total, cash floor ${cfg.cash_floor:g}")
    while True:
        t0 = time.time()
        try:
            out = bot.scan()
            sk = ", ".join(f"{k} {v}" for k, v in sorted(out["skips"].items()))
            bot.log(f"scan: {out['markets']} props, {out['signals']} outside the "
                    f"band, {len(out['taken'])} taken"
                    + (f"; skipped: {sk}" if sk else "")
                    + (f"; errors: {out['errors']}" if out["errors"] else "")
                    + ("; HALTED" if out.get("halted") else "")
                    + f"; book ${out['book']['risk_total']:.2f} at risk")
        except KeyboardInterrupt:
            bot.log("stopped")
            return 0
        except Exception as ex:                          # noqa: BLE001
            bot.log(f"! scan failed: {type(ex).__name__}: {str(ex)[:300]}")
        if args.once:
            return 0
        if until is not None and time.time() >= until:
            bot.log(f"--until {args.until} reached; stopping")
            return 0
        try:
            time.sleep(max(1.0, cfg.scan_secs - (time.time() - t0)))
        except KeyboardInterrupt:
            bot.log("stopped")
            return 0


if __name__ == "__main__":
    sys.exit(main())
