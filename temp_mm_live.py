#!/usr/bin/env python3
"""LIVE executor for the hourly-temp market maker (KXTEMPMIAH) -- DRY by default.

Jack 2026-10-15: "yes build F, which is a superset of G right?" -- after the
shadow test (TEMP_SHADOW.md) showed variant F_full netting ~+$73/hour. This
process quotes Kalshi's hourly Miami temperature markets with THE SAME
policy code the shadow replays (temp_mm_policy.decide), so the shadow's
numbers describe what trades. Every component is switchable: --variant G or
J (or any temp_mm_policy.VARIANTS key) and TMM_<POLICY_KEY> env overrides.
Handoff, commands and limits: TEMP_MM_LIVE.md.

DRY BY DEFAULT. Everything runs against live data (WebSocket books, the
station feed, programs, markets), but create / amend / decrease / cancel are
logged as "[DRY] would ..." and a dry book tracks the orders that would rest
(no fills are simulated: the shadow does the scoring). --live sends orders
and refuses to start without --subaccount N (N > 0): Jack's rule -- its own
numbered Kalshi subaccount, never the IMM's primary. Jack alone creates and
funds the subaccount and flips dry -> live (TEMP_MM_LIVE.md).

One process, event-driven (~50 ms wake, a full decision at least every
loop_s = 250 ms and at once on a new station row, a book change or a fill):
  books + our fills   kalshi_ws.KalshiFeed, one WebSocket signed with
                      kalshi_reads' key (the account that owns the orders);
                      the current and next KXTEMPMIAH events' markets; fills
                      matched by order id / client_order_id prefix "tmm-"
  station readings    the shadow collector's wx stream (tailed read-only);
                      when it is stale (file missing, or > 20 s without a
                      row inside :13-:50 while the collector's own heartbeat
                      is not fresh) this process polls
                      /live_data/weather/miami itself, unsigned, on the
                      collector's cadence (1 s inside :13-:50, 5 s outside,
                      2 s for 2 min after a 429, backing off 2 .. 60 s); at
                      start, when the collector's files do not cover the
                      75-min lookback, the history route seeds the tape
  fair                temp_shadow_model WxTape + FairModel (the shadow's
                      model_params.json, or TMM_MODEL_PARAMS), recomputed
                      on every new wx row; stale (pads only) until the
                      station history spans an hour; live refuses the
                      built-in default model, the hourly reload never falls
                      back to it
  markets / programs  REST discovery every 45 s (min_close_ts), active
                      incentive programs every 15 s in hh:00-06 (they start
                      ~2-3 min after the open; every 5 s while an open event
                      has none, up to 5 min), else 5 min; reader thread
  each decision       per event: MarketStates (external book = WS book minus
                      our resting orders) -> temp_mm_policy.decide -> guards
                      -> an order-level plan: amend in place for a new
                      price (never through the last-read book: amend has no
                      post_only), decrease for a smaller size, a new order
                      for an increase (the sim's back-of-queue lot), cancel
                      what is no longer wanted, post_only create for new
                      prices; expiry renewed by an expiry-only amend before
                      the TTL (90 s; 1c pads 300 s) runs out
  urgent vs paced     a cancel, a size cut, or a reprice down from above the
                      fair guard's cap goes out at once (cancels batched);
                      everything else waits for min_act_s (1 s, the sim's
                      tick) per market and the write budget (3/s, burst 6;
                      60 new orders and 180 writes a minute); a reprice UP
                      (more aggressive) waits until the new price has held
                      2 s, a reprice down never waits; a size cut that comes
                      with a reprice goes first, at once; two or more
                      guard-forced reprices go as one batched cancel + post-
                      only re-places; an order only moves to a price of its
                      own kind (a favorite giving way to a 1c pad is a pull)
  pad hold            policy pad_hold_open_s (G: 180; Jack 2026-10-16: "hold
                      the pads for the first 3 minutes of each hour going
                      forward"): no 1c longshot / qualification pads while
                      the orders would start resting before the event's open
                      (EventInfo.open_ts, the markets' open_time) + the hold;
                      the bot decides on the first wake past that edge and
                      the pads are new creates (never debounced; paced like
                      any create); 1c pads do not count in the debounce's
                      price key, so they never delay a favorite's move
  quote hours         policy quote_hours_et (FAV_night: settle hours 20-23,
                      0-7 ET; Jack 2026-10-16, "1"): an event whose
                      settlement (EventInfo.settle_ts, the ticker's hour)
                      falls in an ET hour not listed rests nothing at all --
                      decide() fails every market's program test (reason
                      "off_hours"), so anything resting there is cancelled;
                      the same rule the shadow sim applies. temp_mm_policy.
                      et_hour is DST-safe (no tz files); logged once per
                      event, guard "off_hours", status events.*.quote_hour.
                      TMM_QUOTE_HOURS_ET="20,21,22,23,0,1,2,3,4,5,6,7"
                      (or "all" / "none" for every hour) overrides it
  market agreement    policy fav_market_min_p (FAV_night_agree: 0.95; Jack
                      2026-10-17, "1"): a favorite join only when the
                      market's favorite probability on the EXTERNAL book
                      (the WS book minus our resting orders; the dry book
                      is not subtracted) is >= it -- two-sided mid, (best
                      favorite bid + 1) / 100 when the longshot side is
                      empty, no join when the favorite side is empty
                      (temp_mm_policy.market_fav_prob, inside decide(): the
                      shadow sim's rule). A join the market stops agreeing
                      with is cancelled at once (a cancel is urgent).
                      Decision samples log fav_market {side, p, min, ok}.
                      TMM_FAV_MARKET_MIN_P=0.95 (or "off") overrides it

Risk and safety (TEMP_MM_LIVE.md has every default):
  pos_cap / event_wcl_cap   the policy's pre-trade sizing on FILLED positions
                            (what RESTS can lose more if only the losing side
                            fills: TMM_WCL_RESTING=1 shares one budget; the
                            subaccount's cash is the hard bound)
  event loss halt           realized + MTM <= -$200 on one event: cancel all
                            its orders (pads too) for the rest of the hour
  daily loss halt           <= -$300 over the ET day: cancel all, write HALT,
                            email, exit 3 (the launcher stops)
  cash                      live refuses to start on a $0 subaccount; an
                            order the cash cannot fund is trimmed or skipped
  staleness                 WS unhealthy -> cancel everything; a book
                            untrusted -> cancel that market; station data
                            stale (no new minute for 3 min) -> live, near and
                            favorite quotes pulled (1c pads stay); no fair ->
                            no quotes on that market; no program -> none;
                            after close -> nothing
  HALT file                 KL-data\\temp-mm\\HALT: cancel all and idle (a dry
                            run's own halts write dry\\HALT, never this one)
  shutdown                  Ctrl+C / SIGBREAK / SIGTERM / atexit cancel every
                            tmm- order; the order TTL is the backstop
  startup                   stray tmm- orders in the subaccount are cancelled
  read-back                 every live order is confirmed booked to the
                            intended subaccount (the first one synchronously
                            before any other is placed, the rest in batched
                            reads within ~10 s): subaccount_number == N, or
                            listed by GetOrders?subaccount=N (GetOrder is not
                            scoped). A mismatch -> HALT, the order cancelled
                            where it booked and read back; an order a working
                            read cannot place in 60 s -> HALT; while the reads
                            fail, new orders are held instead
  reconcile                 resting orders every 60 s (an untracked tmm-
                            order is cancelled; non-tmm orders are never
                            touched; an order still resting 10 s after Kalshi
                            answered our cancel -> HALT; a create that got no
                            answer is adopted if it rests), fills every 120 s,
                            positions every 5 min for every event traded
  missed fills              an order of ours that vanished, a cancel / amend
                            that finds it gone, Kalshi's fill count ahead of
                            ours (counted in the risk at once), or the
                            WebSocket coming back make the market suspect: no
                            new orders there until a fills sweep and a
                            positions read agree; the same mismatch after a
                            fills sweep -> HALT (fills are not arriving)
  write failures            429 / 409 (paused) / 5xx / network: no writes for
                            0.5 s doubling to 8 s, resumed by the first
                            success, never counted; email after 10 min. Other
                            rejects: 10 in 5 min -> email, 40 -> HALT; a refused
                            urgent reprice or size cut cancels the order

CLI:
  python temp_mm_live.py                         dry run, variant F
  python temp_mm_live.py --variant G             G (or J, K, ... or a full name)
  python temp_mm_live.py --duration 900          stop after 15 min
  python temp_mm_live.py --live --subaccount N   LIVE (Jack only)
  python temp_mm_live.py --live --subaccount N --cancel-all   (writes HALT, runs
                                                 beside a live bot: no lock)
  python temp_mm_live.py --print-config
Env: TMM_<FIELD> for every Config field and every temp_mm_policy key
(TMM_Q_LIVE=300, TMM_EVENT_WCL_CAP=500, TMM_WCL_RESTING=1, TMM_DAILY_LOSS_HALT=150 ...);
TEMP_MM_DATA moves the data dir; TMM_EMAIL=0 silences email.
Exit codes: 0 done, 1 crash, 2 refused to start, 3 halted.
Logs (live in KL-data\\temp-mm\\, dry in KL-data\\temp-mm\\dry\\):
tmm_<UTC day>.log, orders_ / fills_ / decisions_<UTC day>.jsonl,
status.json (heartbeat), summary.jsonl (one line per ET day), state.json.
"""
from __future__ import annotations

import argparse
import atexit
import datetime as _dt
import json
import math
import os
import queue
import signal
import smtplib
import sys
import threading
import time
import uuid
from collections import Counter, deque
from dataclasses import asdict, dataclass, field, fields
from email.mime.text import MIMEText
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

import temp_mm_policy as pol
import temp_shadow_common as C

VERSION = "1"
SERIES = "KXTEMPMIAH"
CITY = "miami"
DATA_DIR = os.environ.get("TEMP_MM_DATA", r"C:\Users\jackd\Documents\KL-data\temp-mm")
# orders and account reads: Kalshi's production host (nfl_snipe_bot's finding)
API_BASE = os.environ.get("TMM_API_BASE", "https://external-api.kalshi.com/trade-api/v2")
PUBLIC_BASE = C.API_BASE                 # live_data fallback (unsigned)
WS_URL = C.WS_URL
WS_PATH = "/trade-api/ws/v2"
CLIENT_PREFIX = "tmm"
EXIT_OK, EXIT_ERROR, EXIT_REFUSED, EXIT_HALTED = 0, 1, 2, 3
EPS = 1e-6
WX_LAST_SEC = 420
WX_LO, WX_HI = 13.0, 50.0                # the collector's fast-poll window
WX_LOOKBACK_S = 4500.0                   # the sim's wx lookback (75 min)
OFFICIAL_LAG_S = 330.0                   # a minute is final ~305-317 s after it
ERR_WINDOW_S = 300.0
PASSIVE_TAGS = frozenset(("pad", "qual"))
QUOTE_TAGS_OFF_WHEN_STALE = ("live", "near", "fav")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

@dataclass
class Config:
    """Executor settings (the policy's own are temp_mm_policy keys). Every
    field is env-overridable as TMM_<FIELD>. The mode (--live) and the
    subaccount are CLI-only on purpose: no env var can flip dry -> live."""
    variant: str = "F_full"
    loop_s: float = 0.25            # a full decision at least this often
    wake_s: float = 0.05            # event poll granularity
    min_act_s: float = 1.0          # paced (non-urgent) changes per market
    pull_lead_s: float = 1.0        # orders rest [now+latency, +this): window lookahead
    close_stop_s: float = 1.0       # no orders this close to the close
    order_ttl_s: float = 90.0       # expiration_ts of live / near / fav orders
    pad_ttl_s: float = 300.0        # of 1c pads and qualification pads
    ttl_renew_s: float = 25.0       # renew (expiry-only amend) this long before expiry
    ttl_urgent_s: float = 8.0       # ... and unpaced inside this (queued after the
                                    # guard-forced moves)
    amend_price: bool = True        # reprice in place (else cancel + post-only create)
    inc_mode: str = "new"           # size increase: new | amend
    size_tol_abs: float = 5.0       # same-price size changes smaller than the larger
    size_tol_frac: float = 0.03     # of these are skipped (the taper's 1-contract steps)
    reprice_debounce_s: float = 2.0 # a reprice the fair guard does not force waits until
                                    # the new price has held this long (book flicker)
    max_contracts_order: int = 1100
    max_new_orders_min: int = 60
    max_writes_min: int = 180       # creates + amends + decreases (cancels unlimited)
    write_rate: float = 3.0         # paced writes per second ...
    write_burst: float = 6.0        # ... and burst
    read_rate: float = 1.0          # signed REST reads per second (average) ...
    read_burst: float = 5.0
    daily_loss_halt: float = 300.0  # dollars, ET day, realized + MTM
    event_loss_halt: float = 200.0  # dollars, one event, realized + MTM
    cash_reserve: float = 0.0       # dollars of the subaccount never committed
    require_fair: bool = True       # no model fair -> no quotes on that market
    require_program: bool = True    # no program known -> no quotes
    stale_wx_s: float = 180.0       # no new station minute for this long -> stale
    wx_tail_stale_s: float = 20.0
    wx_poll_gap_s: float = 1.0      # fallback station poll inside :13-:50 (the collector's)
    wx_slow_gap_s: float = 5.0      # ... outside it
    wx_yield_gap_s: float = 2.0     # ... and inside it for wx_yield_s after a 429
    wx_yield_s: float = 120.0
    min_wx_span_s: float = 3600.0   # the fair is stale (pads only) until the station
                                    # history spans this long (trend, rv, anom need it)
    model_params: str = ""          # model_params.json path ("" = the shadow's)
    calibrations: str = ""          # calibrations.json path ("" = the shadow's)
    ws_stale_s: float = 15.0
    markets_s: float = 45.0
    programs_fast_s: float = 15.0   # hh:00-06, when the hour's programs start
    programs_eager_s: float = 5.0   # an event open <= 5 min without its programs
    programs_slow_s: float = 300.0
    balance_s: float = 60.0
    orders_reconcile_s: float = 60.0
    fills_sweep_s: float = 120.0
    positions_s: float = 300.0
    verify_s: float = 5.0           # while a market is suspect: fills + positions this often
    confirm_s: float = 10.0
    confirm_timeout_s: float = 60.0
    decisions_s: float = 60.0
    status_s: float = 5.0
    order_error_alert: int = 10     # order rejects (4xx other than 404/409/429) in 5 min
    order_error_halt: int = 40
    backoff_min_s: float = 0.5      # 429 / 409 paused / 5xx / network on a write: no
    backoff_max_s: float = 8.0      # writes for this long, doubling to the max
    unhealthy_alert_s: float = 600.0  # email once when writes have failed this long
    op_cool_s: float = 5.0          # a rejected op is not resent for this long
    write_timeout_s: float = 3.0    # HTTP read timeout of writes (connect 2 s)
    read_timeout_s: float = 10.0    # ... of the reader thread's signed reads
    read_attempts: int = 2          # ... and their attempts
    sync_read_timeout_s: float = 3.0  # reads on the trading thread (first read-back)
    email: bool = True

    @classmethod
    def from_env(cls, environ: Optional[Dict[str, str]] = None) -> "Config":
        env = os.environ if environ is None else environ
        cfg = cls()
        for f in fields(cls):
            raw = env.get("TMM_" + f.name.upper())
            if raw is None or str(raw).strip() == "":
                continue
            setattr(cfg, f.name, _parse_like(getattr(cfg, f.name), raw))
        return cfg


_CFG_NAMES = {f.name for f in fields(Config)}
assert not (_CFG_NAMES & set(pol.DEFAULT_POLICY)), "Config / policy env names collide"


def parse_hours(raw: Any) -> Optional[List[int]]:
    """TMM_QUOTE_HOURS_ET: "20,21,0,1" or "[20, 21, 0, 1]" (commas / spaces)
    -> [20, 21, 0, 1]; "all" / "any" -> None (every hour). An unset or empty
    variable never reaches here (policy_overrides_from_env skips it: the
    variant's own quote_hours_et stays). ValueError (the bot refuses to
    start) on anything that is not a whole hour 0..23, and on "none" /
    "null" / "[]" / an empty value: in the policy an empty list quotes NO
    hour while None quotes every hour, so those spellings are ambiguous
    (fix pass 10/16: "[]" and "none" used to mean every hour, turning
    FAV_night into 24-hour quoting). To quote nothing, do not start the bot."""
    s = str(raw).strip()
    if s.lower() in ("all", "any"):
        return None
    if s.lower() in ("none", "null"):
        raise ValueError(f"TMM_QUOTE_HOURS_ET={s!r} is ambiguous: 'all' quotes every hour, "
                         "a list of ET hours quotes those; to quote nothing do not start the bot")
    inner = s[1:-1].strip() if s.startswith("[") and s.endswith("]") else s
    parts = inner.replace(",", " ").split()
    if not parts:
        raise ValueError(f"TMM_QUOTE_HOURS_ET={s!r}: an empty list would quote no hour; "
                         "'all' quotes every hour; to quote nothing do not start the bot")
    out = []
    for part in parts:
        if not part.isdigit() or not 0 <= int(part) <= 23:
            raise ValueError(f"TMM_QUOTE_HOURS_ET: {part!r} is not an ET hour 0..23")
        out.append(int(part))
    return out


def parse_prob_or_none(raw: Any) -> Optional[float]:
    """TMM_FAV_MARKET_MIN_P: "0.95" -> 0.95; "off" / "none" / "null" -> None
    (the agreement rule off). An unset or empty variable never reaches here
    (the variant's own value stays). ValueError (the bot refuses to start)
    on anything else; the range (0..1) is temp_mm_policy.check_values'."""
    s = str(raw).strip()
    if s.lower() in ("off", "none", "null"):
        return None
    try:
        return float(s)
    except ValueError:
        raise ValueError(f"TMM_FAV_MARKET_MIN_P={s!r}: a probability 0..1, or 'off'") from None


def _parse_like(default: Any, raw: str) -> Any:
    s = str(raw).strip()
    if isinstance(default, bool):
        return s.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(float(s))
    if isinstance(default, float):
        return float(s)
    return s


def resolve_variant(name: str) -> str:
    """'F' / 'f' / 'F_full' -> 'F_full' (a VARIANTS key or its letter)."""
    n = (name or "").strip()
    if n in pol.VARIANTS:
        return n
    hits = [k for k in pol.VARIANTS if k.split("_", 1)[0].upper() == n.upper()]
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f"unknown variant {name!r}; known: {sorted(pol.VARIANTS)}")


def policy_overrides_from_env(environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    env = os.environ if environ is None else environ
    out = {}
    for k, dv in pol.DEFAULT_POLICY.items():
        raw = env.get("TMM_" + k.upper())
        if raw is None or str(raw).strip() == "":
            continue
        if k == "quote_hours_et":
            out[k] = parse_hours(raw)
        elif k == "fav_market_min_p":       # default None: _parse_like would keep a string
            out[k] = parse_prob_or_none(raw)
        else:
            out[k] = _parse_like(dv, raw)
    return out


def stale_policy(P: dict) -> dict:
    """The policy with live, near-decided and favorite quoting off: what
    rests while station data is stale (1c longshot pads + qualification
    pads only, always at 1c)."""
    Q = dict(P)
    Q.update(live=False, near_decided_half=False, q_fav=0)
    return Q


def ttl_for(cfg: Config, tag: str) -> float:
    parts = {p for p in (tag or "").split("+") if p}
    return cfg.pad_ttl_s if parts and parts <= PASSIVE_TAGS else cfg.order_ttl_s


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def fpf(x, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def utc_day(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%d")


def cents_levels(levels: Optional[Dict[float, float]]) -> Dict[int, float]:
    """A KalshiFeed side ({dollars: size}) -> {cents: size}."""
    out: Dict[int, float] = {}
    for p, q in (levels or {}).items():
        c = int(round(float(p) * 100.0))
        if 1 <= c <= 99 and float(q) > EPS:
            out[c] = out.get(c, 0.0) + float(q)
    return out


def fill_price_cents(f: dict, side: str) -> Optional[int]:
    """The cents (in our side's space) a fill executed at: no_price_dollars
    for a NO fill when present, else from yes_price_dollars (WS and REST
    fills carry it); None when the fill names no price."""
    def cents(v) -> Optional[int]:
        if v in (None, ""):
            return None
        c = int(round(fpf(v, -1.0) * 100.0))
        return c if 1 <= c <= 99 else None
    if side == "no":
        c = cents(f.get("no_price_dollars"))
        if c is not None:
            return c
    yc = cents(f.get("yes_price_dollars"))
    if yc is None:
        return None
    return yc if side == "yes" else 100 - yc


def event_settle_ts(event: str) -> Optional[float]:
    pe = C.parse_event(event)
    return pe[1] if pe else None


def program_params(p: dict) -> Optional[dict]:
    """A REST incentive program -> {start, end, target, df, reward, pool}
    (temp_shadow_sim._program_params' rules)."""
    st = C.parse_iso(p.get("start_date") or "")
    en = C.parse_iso(p.get("end_date") or "")
    if st is None or en is None or en <= st:
        return None
    pr = fpf(p.get("period_reward"))
    tgt = C.fp(p.get("target_size_fp")) or pol.TARGET
    dfb = p.get("discount_factor_bps")
    df = float(dfb) / 10000.0 if dfb is not None else pol.DF
    return dict(start=st, end=en, target=tgt, df=df, reward=pr / 10000.0,
                pool=pr / 10000.0 / (en - st), id=p.get("id"),
                type=p.get("incentive_type"))


class Bucket:
    """Token bucket on the caller's clock. take() can go negative (urgent
    ops are never refused; they delay what is paced after them)."""

    def __init__(self, rate: float, burst: float, now: float = 0.0):
        self.rate, self.cap = float(rate), float(burst)
        self.tokens = float(burst)
        self.t = now

    def _refill(self, now: float) -> None:
        if now > self.t:
            self.tokens = min(self.cap, self.tokens + (now - self.t) * self.rate)
            self.t = now

    def try_take(self, now: float, n: float = 1.0) -> bool:
        self._refill(now)
        if self.tokens >= n - EPS:
            self.tokens -= n
            return True
        return False

    def force_take(self, now: float, n: float = 1.0) -> None:
        self._refill(now)
        self.tokens -= n

    def wait_s(self, now: float, n: float = 1.0) -> float:
        self._refill(now)
        return 0.0 if self.tokens >= n else (n - self.tokens) / max(self.rate, 1e-9)


class CallStats:
    """REST calls by kind: totals and the last 60 s (thread-safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self.total: Counter = Counter()
        self._recent: Deque[Tuple[float, str]] = deque()

    def add(self, kind: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self.total[kind] += 1
            self._recent.append((now, kind))
            while self._recent and self._recent[0][0] < now - 60.0:
                self._recent.popleft()

    def last_minute(self, now: Optional[float] = None) -> Dict[str, int]:
        now = time.time() if now is None else now
        with self._lock:
            while self._recent and self._recent[0][0] < now - 60.0:
                self._recent.popleft()
            return dict(Counter(k for _, k in self._recent))


class ApiError(Exception):
    """A failed Kalshi call. status = the HTTP status, None when no answer
    came back; net = no answer for a network reason (timeout, connection):
    the request may or may not have reached Kalshi. net defaults to True
    when status is None (a fake exchange raising ApiError(status=None)
    stands for a timeout)."""

    def __init__(self, msg: str, status: Optional[int] = None, body: str = "",
                 net: Optional[bool] = None):
        super().__init__(msg)
        self.status = status
        self.body = body
        self.net = (status is None) if net is None else bool(net)


def _is_network_error(e: BaseException) -> bool:
    try:
        import requests
        if isinstance(e, requests.exceptions.RequestException) and getattr(e, "status", None) is None:
            return True
    except ImportError:                                  # pragma: no cover
        pass
    return isinstance(e, (TimeoutError, ConnectionError))


def _api_error(e: BaseException) -> ApiError:
    if isinstance(e, ApiError):
        return e
    status = getattr(e, "status", None)
    return ApiError(f"{type(e).__name__}: {str(e)[:200]}", status,
                    str(getattr(e, "body", "") or "")[:500],
                    net=(status is None and _is_network_error(e)))


def err_class(e: ApiError) -> str:
    """'gone' (404: the order is not there), 'throttle' (429), 'paused'
    (409: Kalshi did not process it -- trading_is_paused / exchange_is_paused;
    the V2 cancel and amend docs list no 409), 'unavailable' (5xx, or no
    answer for a network reason: may or may not have landed), 'reject' (any
    other 4xx, or a local error: counted toward the error halt)."""
    st = e.status
    if st == 404:
        return "gone"
    if st == 429:
        return "throttle"
    if st == 409:
        return "paused"
    if (st is None and e.net) or (st is not None and st >= 500):
        return "unavailable"
    return "reject"


TRANSIENT = frozenset(("throttle", "paused", "unavailable"))


def item_error_class(err: Any) -> str:
    """A batch-cancel item's error object -> err_class's names."""
    txt = json.dumps(err, default=str).lower() if err else ""
    if "not_found" in txt or "not found" in txt:
        return "gone"
    if "paused" in txt:
        return "paused"
    if "rate" in txt and "limit" in txt:
        return "throttle"
    return "reject"


def parse_ts(v: Any) -> Optional[float]:
    """Epoch seconds from an epoch number / numeric string / ISO string."""
    if v in (None, ""):
        return None
    try:
        x = float(v)
        return x / 1000.0 if x > 1e11 else x
    except (TypeError, ValueError):
        return C.parse_iso(str(v))


# --------------------------------------------------------------------------
# Exchange (Kalshi REST)
# --------------------------------------------------------------------------

def _v2_side(side: str) -> str:
    """Our bid side -> the V2 YES-book side: a YES bid is 'bid', a NO bid
    at q is an 'ask' (sell YES) at 100 - q."""
    return "bid" if side == "yes" else "ask"


def _yes_cents(side: str, price: int) -> int:
    return int(price) if side == "yes" else 100 - int(price)


class Exchange:
    """Kalshi REST for the executor: signed with kalshi_reads' key (the
    IMM's account, the one the subaccount belongs to) on the production
    host. Market lists go through kalshi_reads (signed, paced per process).
    WRITES REFUSE unless built with live=True -- main() does that only after
    --live --subaccount N and the cash check."""

    def __init__(self, subaccount: int, live: bool = False, api_base: str = API_BASE,
                 stats: Optional[CallStats] = None, read_rate: float = 1.0,
                 read_burst: float = 5.0, write_timeout_s: float = 3.0,
                 read_timeout_s: float = 10.0, read_attempts: int = 2,
                 sync_read_timeout_s: float = 3.0):
        self.sub = int(subaccount)
        self.live = bool(live)
        self.api_base = api_base
        self.stats = stats or CallStats()
        self._clients: Dict[str, Any] = {}
        self._rb = Bucket(read_rate, read_burst, time.time())
        self._rb_lock = threading.Lock()
        # per-role network bounds (the base client's 5 s / 20 s x 3 GET
        # attempts would let one hung call block the trading thread -- and
        # every urgent cancel behind it -- for a minute): writes 2 s connect /
        # write_timeout_s read, single shot; reader-thread reads bounded and
        # tried read_attempts times; trading-thread reads ("s") one short try
        self.bounds = {"w": ((2.0, float(write_timeout_s)), 1),
                       "r": ((5.0, float(read_timeout_s)), max(1, int(read_attempts))),
                       "s": ((2.0, float(sync_read_timeout_s)), 1),
                       "ws": ((5.0, 20.0), 1)}

    # -- plumbing ----------------------------------------------------------
    def client(self, role: str = "r"):
        c = self._clients.get(role)
        if c is None:
            os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")
            import kalshi_reads as kr
            from KalshiClientsBaseV2ApiKey_FIXED import ExchangeClient
            c = ExchangeClient(exchange_api_base=self.api_base, key_id=kr.KEY_ID,
                               private_key=kr.load_private_key())
            self._clients[role] = self.apply_bounds(c, role)
        return c

    def apply_bounds(self, c: Any, role: str) -> Any:
        """The role's (connect, read) timeout and GET attempts on a client
        (the base client reads both per instance)."""
        timeout, attempts = self.bounds.get(role, self.bounds["r"])
        c.http_timeout = timeout
        c.get_attempts = attempts
        return c

    def ws_headers(self) -> Callable[[], Dict[str, str]]:
        c = self.client("ws")

        def headers() -> Dict[str, str]:
            ts = str(int(time.time() * 1000))
            return {"KALSHI-ACCESS-KEY": c.key_id,
                    "KALSHI-ACCESS-SIGNATURE": c.sign_pss_text(ts + "GET" + WS_PATH),
                    "KALSHI-ACCESS-TIMESTAMP": ts}
        return headers

    def _pace_read(self) -> None:
        while True:
            with self._rb_lock:
                w = self._rb.wait_s(time.time())
                if w <= 0:
                    self._rb.force_take(time.time())
                    return
            time.sleep(min(w, 1.0))

    def _sub(self, params: Optional[dict] = None) -> dict:
        p = dict(params or {})
        if self.sub:
            p["subaccount"] = self.sub
        return p

    def _get(self, kind: str, path: str, params: dict, role: str = "r") -> Any:
        self._pace_read()
        self.stats.add("r_" + kind)
        try:
            return self.client(role).get(path, params)
        except Exception as e:                           # noqa: BLE001
            raise _api_error(e)

    def _paged(self, kind: str, path: str, params: dict, key: str,
               max_pages: int = 20) -> List[dict]:
        out: List[dict] = []
        cursor = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            js = self._get(kind, path, p) or {}
            items = js.get(key) or []
            out += items
            cursor = js.get("cursor") or js.get("next_cursor")
            if not cursor or not items:
                break
        return out

    def _md(self, kind: str, path: str, params: dict) -> Any:
        """A market-data read through kalshi_reads (signed first)."""
        from kalshi_reads import kalshi_get
        self._pace_read()
        self.stats.add("r_" + kind)
        return kalshi_get(path, params)

    # -- market data -------------------------------------------------------
    def markets(self, min_close_ts: float) -> List[dict]:
        out: List[dict] = []
        cursor = None
        for _ in range(10):
            p = {"series_ticker": SERIES, "min_close_ts": int(min_close_ts), "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            js = self._md("markets", "/markets", p) or {}
            items = js.get("markets") or []
            out += items
            cursor = js.get("cursor")
            if not cursor or not items:
                break
        return out

    def event_markets(self, event: str) -> List[dict]:
        js = self._md("event_markets", "/markets", {"event_ticker": event, "limit": 1000}) or {}
        return js.get("markets") or []

    def programs(self, now: float) -> List[dict]:
        """Active KXTEMPMIAH liquidity programs. Pages come newest-first by
        start date (the collector's verified early stop): a page reaching
        2 h before now ends the sweep -- 1 read (measured 10/16 02:02Z:
        page 1 already reached 8.8 h back). status=upcoming is not read: it
        never listed a KXTEMPMIAH program (they appear at their start, ~2-3
        min after the open) and costs 3 pages, soonest last."""
        out: List[dict] = []
        prefix = SERIES + "-"
        cursor = None
        for _ in range(10):
            p = {"status": "active", "limit": 1000}
            if cursor:
                p["cursor"] = cursor
            js = self._md("programs", "/incentive_programs", p) or {}
            batch = js.get("incentive_programs") or []
            out += [x for x in batch if str(x.get("market_ticker", "")).startswith(prefix)]
            cursor = js.get("next_cursor")
            if not cursor or not batch:
                break
            starts = [C.parse_iso(x.get("start_date") or "") for x in batch]
            starts = [s for s in starts if s]
            if starts and min(starts) < now - 7200:
                break
        return out

    # -- account reads (subaccount-scoped) ----------------------------------
    def balance(self) -> Optional[float]:
        js = self._get("balance", "/portfolio/balance", self._sub()) or {}
        if self.sub:
            return fpf(js.get("balance_dollars"), 0.0)
        for b in js.get("balance_breakdown") or []:
            if int(b.get("exchange_index", -1)) == 0:
                return fpf(b.get("balance")) / 100.0
        if js.get("balance_dollars") is not None:
            return fpf(js.get("balance_dollars"))
        return fpf(js.get("balance")) / 100.0

    def resting(self) -> List[dict]:
        rows = self._paged("orders", "/portfolio/orders",
                           self._sub({"status": "resting", "limit": 1000}), "orders")
        return [o for o in rows if str(o.get("ticker", "")).startswith(SERIES + "-")]

    def event_orders(self, events: List[str]) -> List[dict]:
        if not events:
            return []
        return self._paged("orders_event", "/portfolio/orders",
                           self._sub({"event_ticker": ",".join(sorted(events)[:10]),
                                      "limit": 1000}), "orders")

    def ticker_orders(self, ticker: str, sync: bool = False) -> List[dict]:
        """GET /portfolio/orders?ticker=..&subaccount=N: the subaccount's
        orders on one market (GetOrders honours `subaccount`; GetOrder does
        not take it)."""
        js = self._get("orders_ticker", "/portfolio/orders",
                       self._sub({"ticker": ticker, "limit": 1000}),
                       role="s" if sync else "r") or {}
        return js.get("orders") or []

    def get_order(self, oid: str, sync: bool = False) -> Optional[dict]:
        """GET /portfolio/orders/{id}. It defines no query params, so it is
        NOT subaccount-scoped: only its subaccount_number says where the
        order booked (None = not reported)."""
        try:
            js = self._get("order", f"/portfolio/orders/{oid}", {},
                           role="s" if sync else "r") or {}
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        return js.get("order") or None

    def fills(self, min_ts: float) -> List[dict]:
        rows = self._paged("fills", "/portfolio/fills",
                           self._sub({"min_ts": int(min_ts), "limit": 1000}), "fills")
        return [f for f in rows
                if str(f.get("ticker") or f.get("market_ticker") or "").startswith(SERIES + "-")]

    def positions(self, event: str) -> List[dict]:
        return self._paged("positions", "/portfolio/positions",
                           self._sub({"event_ticker": event, "limit": 1000}),
                           "market_positions")

    # -- writes (live only) ------------------------------------------------
    def _w(self):
        if not self.live:
            raise RuntimeError("Exchange writes are disabled (dry run)")
        return self.client("w")

    def _call(self, kind: str, fn: Callable[[], Any]) -> Any:
        self.stats.add("w_" + kind)
        try:
            return fn()
        except Exception as e:                           # noqa: BLE001
            raise _api_error(e)

    def _q(self) -> str:
        return f"?subaccount={self.sub}" if self.sub else ""

    def create(self, ticker: str, side: str, price: int, count: int, coid: str,
               expiration_ts: int) -> dict:
        c = self._w()
        kw = dict(ticker=ticker, side=side, action="buy", count=int(count), type="limit",
                  post_only=True, expiration_ts=int(expiration_ts), client_order_id=coid,
                  self_trade_prevention_type="taker_at_cross")
        if side == "yes":
            kw["yes_price"] = int(price)
        else:
            kw["no_price"] = int(price)
        body = c._build_v2_order_body(**kw)
        if self.sub:
            body["subaccount"] = self.sub
        return self._call("create", lambda: c.post(path=c.events_orders_url,
                                                    body=json.dumps(body))) or {}

    def amend(self, o: "Order", price: int, total: float,
              expiration_ts: Optional[int] = None) -> dict:
        """expiration_ts only on a renewal: a reprice leaves it out (the
        order keeps its expiry; the IMM's amends never send it), so a Kalshi
        that refuses to extend an expiry fails only the renewals, which then
        fall back to cancel + re-place."""
        c = self._w()
        body = {"ticker": o.ticker, "side": _v2_side(o.side),
                "price": c._cents_to_dollar_str(_yes_cents(o.side, price)),
                "count": f"{float(total):.2f}"}
        if expiration_ts is not None:
            body["expiration_time"] = int(expiration_ts)
        path = f"{c.events_orders_url}/{o.oid}/amend{self._q()}"
        return self._call("amend", lambda: c.post(path=path, body=json.dumps(body))) or {}

    def decrease(self, o: "Order", reduce_by: int) -> dict:
        c = self._w()
        body = {"reduce_by": f"{float(reduce_by):.2f}", "market_ticker": o.ticker}
        path = f"{c.events_orders_url}/{o.oid}/decrease{self._q()}"
        return self._call("decrease", lambda: c.post(path=path, body=json.dumps(body))) or {}

    def cancel(self, oid: str, ticker: str, subaccount: Optional[int] = None) -> dict:
        """subaccount: where the order booked when that is not ours (a
        mis-booked order found by the read-back); 0 = the primary (the
        param is left out: Kalshi's default)."""
        c = self._w()
        sub = self.sub if subaccount is None else int(subaccount)
        params: Dict[str, Any] = {"exchange_index": -1, "market_ticker": ticker}
        if sub:
            params = {"subaccount": sub, **params}
        return self._call("cancel", lambda: c.delete(path=f"{c.events_orders_url}/{oid}",
                                                     params=params)) or {}

    def batch_cancel(self, items: List[Tuple]) -> List[dict]:
        """DELETE /portfolio/events/orders/batched {"orders": [{order_id,
        subaccount, market_ticker}]} (2 tokens an order). items: (oid,
        ticker) or (oid, ticker, subaccount). The base client's delete()
        cannot send a body, so this one is sent here."""
        c = self._w()
        orders = []
        for it in items:
            oid, t = it[0], it[1]
            sub = self.sub if len(it) < 3 or it[2] is None else int(it[2])
            d = {"order_id": oid, "market_ticker": t}
            if sub:
                d["subaccount"] = sub
            orders.append(d)
        path = c.events_orders_url + "/batched"

        def go():
            c.rate_limit()
            r = c._http().request("DELETE", c.host + path, data=json.dumps({"orders": orders}),
                                  headers=c.request_headers("DELETE", path),
                                  timeout=c.http_timeout)
            c.raise_if_bad_response(r)
            return r.json()
        js = self._call("batch_cancel", go) or {}
        return js.get("orders") or []


def fetch_wx_history(from_ts: float, to_ts: float, timeout: float = 8.0) -> Tuple[int, Any]:
    """GET /live_data/weather/miami?detailed=true&from=<ms>&to=<ms>, UNSIGNED
    (temp_shadow_backfill's route): the station minutes of a past window,
    for seeding the tape when the collector's stream does not cover the
    lookback (a restart after it stopped)."""
    import requests
    r = requests.get(PUBLIC_BASE + f"/live_data/weather/{CITY}",
                     params={"detailed": "true", "from": int(from_ts * 1000),
                             "to": int(to_ts * 1000)}, timeout=timeout)
    try:
        body = r.json() if r.status_code == 200 else None
    except ValueError:
        body = None
    return r.status_code, body


def history_rows(body: Any, now: float, before_rx: Optional[float] = None) -> List[dict]:
    """A detailed history response -> wx stream rows (temp_shadow_model's
    synthetic rx, the way the sim replays a backfill), keeping rows that
    would have arrived by `now` and, with before_rx, only those before the
    collector's first row (the seed fills the gap; it does not overlap)."""
    import temp_shadow_model as tm
    out = []
    for r in tm.synth_rows_from_detailed(tm.detailed_rows(body), CITY):
        rx = float(r.get("rx") or 0.0)
        if rx > now or (before_rx is not None and rx >= before_rx):
            continue
        r.setdefault("city", CITY)
        out.append(r)
    out.sort(key=lambda r: float(r["rx"]))
    return out


# --------------------------------------------------------------------------
# Station readings: the collector's wx stream, with a fallback poller
# --------------------------------------------------------------------------

class WxTail:
    """Tails the shadow collector's wx stream (raw/<UTC day>/wx_<HH>.jsonl
    under TEMP_SHADOW_DATA), READ-ONLY: paths are computed here, never via
    temp_shadow_common.raw_file (which creates directories)."""

    def __init__(self, root: Optional[str] = None, city: str = CITY):
        self.root = root or C.DATA_ROOT
        self.city = city
        self.offsets: Dict[str, int] = {}
        self.bufs: Dict[str, bytes] = {}
        self.last_row_at: Optional[float] = None
        self.rows_read = 0
        self.lags: Deque[float] = deque(maxlen=600)

    def path(self, ts: float) -> str:
        t = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc)
        return os.path.join(self.root, "raw", t.strftime("%Y-%m-%d"), f"wx_{t.strftime('%H')}.jsonl")

    def exists_now(self, now: float) -> bool:
        return os.path.exists(self.path(now))

    def history(self, now: float, lookback: float = WX_LOOKBACK_S) -> List[dict]:
        """Rows with rx in [now - lookback, now] from the closed hours
        (plain or gzipped), then the current hour's file through the tail
        (so its offset is set)."""
        rows: List[dict] = []
        cur = self.path(now)
        h = math.floor((now - lookback) / 3600.0) * 3600.0
        while h <= now:
            p = self.path(h)
            if p != cur:
                for q in (p + ".gz", p):
                    if os.path.exists(q):
                        for r in C.iter_jsonl(q):
                            if self._keep(r) and float(r["rx"]) >= now - lookback:
                                rows.append(r)
            h += 3600.0
        rows += self._read(cur, now, count_lag=False)
        rows.sort(key=lambda r: float(r["rx"]))
        return rows

    def _keep(self, r: dict) -> bool:
        return (isinstance(r, dict) and r.get("t") is not None and r.get("rx") is not None
                and r.get("city", self.city) == self.city)

    def poll(self, now: float) -> List[dict]:
        rows: List[dict] = []
        paths = sorted({self.path(now - 5.0), self.path(now)})
        for p in paths:
            rows += self._read(p, now)
        for p in list(self.offsets):
            if p not in paths:
                self.offsets.pop(p, None)
                self.bufs.pop(p, None)
        return rows

    def _read(self, p: str, now: float, count_lag: bool = True) -> List[dict]:
        try:
            size = os.path.getsize(p)
        except OSError:
            return []
        off = self.offsets.get(p, 0)
        if size < off:
            off = 0
            self.bufs[p] = b""
        if size == off:
            self.offsets[p] = off
            return []
        try:
            with open(p, "rb") as f:
                f.seek(off)
                data = f.read(size - off)
        except OSError:
            return []
        self.offsets[p] = off + len(data)
        buf = self.bufs.get(p, b"") + data
        lines = buf.split(b"\n")
        self.bufs[p] = lines.pop()
        out = []
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln.decode("utf-8"))
            except ValueError:
                continue
            if self._keep(r):
                out.append(r)
                if count_lag:
                    self.lags.append(now - float(r["rx"]))
        if out:
            self.last_row_at = now
            self.rows_read += len(out)
        return out


class WxPoller(threading.Thread):
    """Fallback station feed: GET /live_data/weather/miami?detailed=true&
    last_sec=420 UNSIGNED (the per-IP budget it shares with the collector
    and the IMM; never the signed key), only while engaged, on the
    collector's cadence: one request per gap_s (1 s) inside :13-:50 where
    the readings land, slow_gap_s (5 s) outside; 429 -> back off 2, 4 ..
    60 s and yield (yield_gap_s, 2 s, inside the window) for yield_s
    (120 s). Rows are the collector's shape (temp_shadow_collector.WxDiffer)."""

    def __init__(self, gap_s: float = 1.0, stats: Optional[CallStats] = None,
                 fetch: Optional[Callable[[], Tuple[int, Any]]] = None,
                 slow_gap_s: float = 5.0, yield_gap_s: float = 2.0, yield_s: float = 120.0):
        super().__init__(daemon=True, name="tmm-wx-poll")
        from temp_shadow_collector import WxDiffer
        self.gap_s = max(1.0, float(gap_s))
        self.slow_gap_s = max(self.gap_s, float(slow_gap_s))
        self.yield_gap_s = max(self.gap_s, float(yield_gap_s))
        self.yield_s = float(yield_s)
        self.yield_until = 0.0
        self.stats = stats or CallStats()
        self.fetch = fetch or self._fetch
        self.differ = WxDiffer(CITY)
        self.active = threading.Event()
        self.stop_evt = threading.Event()
        self.q: "queue.Queue[List[dict]]" = queue.Queue()
        self.backoff_until = 0.0
        self.consec_429 = 0
        self.n = self.n429 = self.nerr = 0
        self.last_start = 0.0
        self.last_error = ""

    @staticmethod
    def _fetch() -> Tuple[int, Any]:
        import requests
        r = requests.get(PUBLIC_BASE + f"/live_data/weather/{CITY}",
                         params={"detailed": "true", "last_sec": WX_LAST_SEC}, timeout=5)
        try:
            body = r.json() if r.status_code == 200 else None
        except ValueError:
            body = None
        return r.status_code, body

    def gap(self, now: float) -> float:
        if WX_LO <= now % 60.0 <= WX_HI:
            return self.yield_gap_s if now < self.yield_until else self.gap_s
        return self.slow_gap_s

    def poll_once(self, now: float) -> int:
        """One request if due; returns rows queued (tests call this)."""
        if now < self.backoff_until or now - self.last_start < self.gap(now) - 1e-6:
            return 0
        self.last_start = now
        self.n += 1
        self.stats.add("r_live_data", now)
        t0 = time.perf_counter()
        try:
            status, body = self.fetch()
        except Exception as e:                           # noqa: BLE001
            self.nerr += 1
            self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
            return 0
        rx = now + (time.perf_counter() - t0)            # when the response arrived
        if status == 429:
            self.n429 += 1
            self.consec_429 += 1
            self.backoff_until = now + min(2.0 * 2 ** (self.consec_429 - 1), 60.0)
            self.yield_until = now + self.yield_s
            return 0
        if status != 200 or not isinstance(body, dict):
            self.nerr += 1
            self.last_error = f"HTTP {status}"
            return 0
        self.consec_429 = 0
        rows = self.differ.changed(body, rx)
        if rows:
            self.q.put(rows)
        return len(rows)

    def run(self) -> None:
        while not self.stop_evt.is_set():
            if not self.active.is_set():
                self.stop_evt.wait(0.2)
                continue
            try:
                self.poll_once(time.time())
            except Exception as e:                       # noqa: BLE001
                self.last_error = str(e)[:120]
            self.stop_evt.wait(0.1)

    def drain(self) -> List[dict]:
        out: List[dict] = []
        while True:
            try:
                out += self.q.get_nowait()
            except queue.Empty:
                return out

    def stop(self) -> None:
        self.stop_evt.set()


class WxSource:
    """Station rows for the fair: the collector's stream, the fallback
    poller only while that stream is stale. A row in the last
    wx_tail_stale_s (20 s) = fresh. Otherwise, while the collector's
    heartbeat (collector_status.json, every 10 s) is fresh, the collector is
    trusted: a quiet minute writes no rows, and a collector backing off a
    429 would only be crowded by more polls from this IP -- unless its
    station poll has not succeeded for 60 s and no row came for 90 s. With
    the heartbeat stale (collector dead): the current hour's file missing,
    or no row inside :13-:50, or > 90 s without one = stale."""

    def __init__(self, cfg: Config, tail: Optional[WxTail] = None,
                 poller: Optional[WxPoller] = None, status_path: Optional[str] = None,
                 threaded: bool = True,
                 history_fetch: Optional[Callable[[float, float], Tuple[int, Any]]] = None):
        self.cfg = cfg
        self.tail = tail or WxTail()
        self.poller = poller
        self.status_path = status_path or os.path.join(self.tail.root, "collector_status.json")
        self.threaded = threaded
        self.history_fetch = history_fetch
        self.polling = False
        self.switches = 0
        self._coll: Tuple[float, Tuple[bool, float]] = (-1e18, (False, math.inf))
        self.fallback_rows = 0
        self.seeded_rows = 0
        self.seed_note = ""

    def start(self, now: float) -> List[dict]:
        """The lookback's rows: the collector's files, and -- when they do not
        reach back to within 15 min of the lookback's start (the collector
        stopped, or was down) -- the history route's minutes before them."""
        if self.poller is not None and self.threaded:
            self.poller.start()
        rows = self.tail.history(now)
        first = float(rows[0]["rx"]) if rows else None
        if self.history_fetch is not None and (first is None
                                               or first > now - WX_LOOKBACK_S + 900.0):
            seed = self.seed_rows(now, first)
            rows = sorted(seed + rows, key=lambda r: float(r["rx"]))
        return rows

    def seed_rows(self, now: float, before_rx: Optional[float] = None) -> List[dict]:
        """History-route rows over the lookback, before `before_rx` (never
        raises; seed_note says what happened)."""
        if self.history_fetch is None:
            return []
        try:
            status, body = self.history_fetch(now - WX_LOOKBACK_S, now)
        except Exception as e:                           # noqa: BLE001
            self.seed_note = f"history read failed: {type(e).__name__}: {str(e)[:120]}"
            return []
        if status != 200 or not isinstance(body, dict):
            self.seed_note = f"history read HTTP {status}"
            return []
        rows = history_rows(body, now, before_rx)
        self.seeded_rows += len(rows)
        self.seed_note = f"seeded {len(rows)} rows from the history route"
        return rows

    def collector_state(self, now: float) -> Tuple[bool, float]:
        """(heartbeat fresh, seconds since its last successful station poll)."""
        t, st = self._coll
        if now - t < 5.0:
            return st
        js = C.read_json(self.status_path, None)
        fresh, ok_age = False, math.inf
        if isinstance(js, dict):
            fresh = now - fpf(js.get("rx"), 0.0) <= 30.0 and js.get("state", "running") == "running"
            wx = ((js.get("wx") or {}).get(CITY) or {})
            if wx.get("last_ok_age_s") is not None:
                ok_age = fpf(wx.get("last_ok_age_s"), math.inf) + max(0.0, now - fpf(js.get("rx"), now))
        self._coll = (now, (fresh, ok_age))
        return fresh, ok_age

    def collector_alive(self, now: float) -> bool:
        return self.collector_state(now)[0]

    def tail_stale(self, now: float) -> bool:
        last = self.tail.last_row_at
        age = math.inf if last is None else now - last
        if age <= self.cfg.wx_tail_stale_s:
            return False
        fresh, ok_age = self.collector_state(now)
        if fresh:
            return ok_age > 60.0 and age > 90.0
        if not self.tail.exists_now(now):
            return True
        if WX_LO <= now % 60.0 <= WX_HI:
            return True
        if age > 90.0:
            return True
        return self.polling

    def poll(self, now: float) -> List[dict]:
        rows = self.tail.poll(now)
        if self.poller is not None:
            want = self.tail_stale(now)
            if want != self.polling:
                self.polling = want
                self.switches += 1
                (self.poller.active.set if want else self.poller.active.clear)()
            if self.polling and not self.threaded:
                self.poller.poll_once(now)
            extra = self.poller.drain()
            self.fallback_rows += len(extra)
            rows += extra
        return rows

    def status(self, now: float) -> dict:
        lags = sorted(self.tail.lags)
        d = {"source": "poll" if self.polling else "collector",
             "last_row_age_s": (round(now - self.tail.last_row_at, 1)
                                if self.tail.last_row_at else None),
             "rows_read": self.tail.rows_read, "fallback_rows": self.fallback_rows,
             "switches": self.switches, "seeded_rows": self.seeded_rows,
             "tail_lag_s_p50": round(lags[len(lags) // 2], 3) if lags else None,
             "tail_lag_s_max": round(lags[-1], 3) if lags else None}
        if self.poller is not None:
            d.update(poll_n=self.poller.n, poll_429=self.poller.n429, poll_err=self.poller.nerr,
                     poll_last_error=self.poller.last_error)
        return d

    def stop(self) -> None:
        if self.poller is not None:
            self.poller.stop()


@dataclass
class FairResult:
    fairs: Dict[str, float]           # ticker -> P(YES)
    slopes: Dict[str, float]          # ticker -> guard slope (cents per 0.36 F)
    stale: bool
    reason: str = ""
    latest_minute_ms: Optional[int] = None
    index: Optional[float] = None
    mu: Optional[float] = None
    sigma: Optional[float] = None


class FairEngine:
    """WxTape + FairModel over the last 75 min of wx rows (the sim's
    lookback), the shadow's model_params.json (or params_path). The fair
    moves only with new rows (the model is relative to the latest minute),
    so it is cached per snapshot. Stale when no new station minute arrived
    for stale_wx_s, the newest minute is older than stale_wx_s + the ~330 s
    publication lag, or the history spans less than min_span_s (a restart
    without the collector's files: trend, rv and anom need the hour).
    params_source / cals_source say where the model came from ('defaults' =
    the built-in fallback, not the model the shadow validated: live refuses
    it, and the hourly reload keeps the loaded model rather than fall back)."""

    def __init__(self, model=None, calibrations=None, params_path: str = "",
                 cals_path: str = "", min_span_s: float = 0.0):
        import temp_shadow_model as tm
        self.tm = tm
        self.params_path = params_path or ""
        self.cals_path = cals_path or ""
        self.min_span_s = float(min_span_s)
        self.model = model if model is not None else self._load_model()
        self.cals = (calibrations if calibrations is not None
                     else tm.Calibrations.load(self.cals_path or None))
        self.unknown_cal_versions: List[str] = []
        self._first_t: Tuple[int, Tuple[Optional[int], Optional[int]]] = (-1, (None, None))
        self.rows: List[dict] = []
        self.version = 0
        self._tape = None
        self._tape_ver = -1
        self._built_at = 0.0
        # settle_ts -> (snapshot, fair, slopes, dist): a hit needs the SAME
        # snapshot object (held here, so its id cannot be reused)
        self._cache: Dict[float, Tuple[Any, Dict[float, float], Dict[float, float], Any]] = {}
        self.latest_minute_ms: Optional[int] = None
        self.latest_adv_at: Optional[float] = None
        self.loaded_at = time.time()
        self.params_mtime = self._mtime()
        self.errors = 0

    def _load_model(self):
        return self.tm.FairModel(self.params_path) if self.params_path else self.tm.FairModel()

    @property
    def params_source(self) -> str:
        return str(getattr(self.model, "params_source", "?"))

    @property
    def cals_source(self) -> str:
        return str(getattr(self.cals, "source", "?"))

    def _mtime(self) -> Optional[float]:
        try:
            return os.path.getmtime(self.params_path or C.MODEL_PARAMS_FILE)
        except OSError:
            return None

    def maybe_reload(self, now: float) -> Optional[str]:
        """Hourly: a refit model_params.json / new calibrations are picked
        up. A file that disappeared or no longer parses never replaces a
        loaded model with the built-in defaults. Returns a line to log
        ('!' first = alert), or None."""
        if now - self.loaded_at < 3600.0:
            return None
        self.loaded_at = now
        m = self._mtime()
        changed = m != self.params_mtime
        self.params_mtime = m
        notes = []
        try:
            cals = self.tm.Calibrations.load(self.cals_path or None)
            if cals.source == "defaults" and self.cals_source != "defaults":
                notes.append(f"! calibrations unreadable or gone: keeping {self.cals_source}")
            else:
                self.cals = cals
            if changed:
                mdl = self._load_model()
                if mdl.params_source == "defaults" and self.params_source != "defaults":
                    notes.append(f"! model params unreadable or gone "
                                 f"({self.params_path or C.MODEL_PARAMS_FILE}): keeping the "
                                 f"model loaded from {self.params_source}")
                else:
                    self.model = mdl
                    notes.append(f"model params reloaded from {mdl.params_source} (file changed)")
        except Exception as e:                           # noqa: BLE001
            self.errors += 1
            notes.append(f"! model reload failed: {type(e).__name__}: {str(e)[:120]}")
        self.version += 1
        notes.sort(key=lambda n: not n.startswith("!"))
        return "; ".join(notes) or None

    def _minutes(self) -> Tuple[Optional[int], Optional[int]]:
        key = self.version * 1000003 + len(self.rows)
        if self._first_t[0] != key:
            ts = [int(r["t"]) for r in self.rows if r.get("t") is not None]
            self._first_t = (key, (min(ts), max(ts)) if ts else (None, None))
        return self._first_t[1]

    def history_span_s(self) -> float:
        """Minutes the station rows span (first to last minute), seconds."""
        f, l = self._minutes()
        return 0.0 if f is None else max(0.0, (l - f) / 1000.0)

    def short_history(self) -> bool:
        return self.min_span_s > 0 and self.history_span_s() < self.min_span_s

    def first_rx(self) -> Optional[float]:
        return float(self.rows[0]["rx"]) if self.rows else None

    REBUILD_S = 1800.0

    def add(self, rows: List[dict], now: float) -> None:
        """New wx rows. Rows arriving in rx order are appended to the live
        WxTape (its snapshot applies only the new rows: ~1 ms, not a
        75-minute rebuild); out-of-order rows, and every 30 min (to drop
        rows past the lookback), rebuild it."""
        rows = sorted((r for r in rows if r.get("t") is not None and r.get("rx") is not None
                       and r.get("city", CITY) in (None, CITY)), key=lambda r: float(r["rx"]))
        if not rows:
            return
        in_order = not self.rows or float(rows[0]["rx"]) >= float(self.rows[-1]["rx"])
        self.rows.extend(rows)
        if not in_order:
            self.rows.sort(key=lambda r: float(r["rx"]))
        lo = now - WX_LOOKBACK_S - 600.0
        tape = self._tape
        if (in_order and tape is not None and self._tape_ver == self.version
                and now - self._built_at < self.REBUILD_S):
            for r in rows:
                tape.rows.append(r)
                tape._rxs.append(float(r["rx"]))
            return
        if self.rows and float(self.rows[0]["rx"]) < lo:
            self.rows = [r for r in self.rows if float(r["rx"]) >= lo]
        self.version += 1

    def snapshot(self, now: float):
        if self._tape_ver != self.version or self._tape is None:
            self._tape = self.tm.WxTape(self.rows, calibrations=self.cals)
            self._tape_ver = self.version
            self._built_at = now
            self._cache.clear()
            self.unknown_cal_versions = list(getattr(self._tape, "unknown_cal_versions", []) or [])
        snap = self._tape.snapshot(now)
        L = snap.latest_minute_ms
        if L is not None and (self.latest_minute_ms is None or L > self.latest_minute_ms):
            self.latest_minute_ms = int(L)
            self.latest_adv_at = now
        return snap

    def stale(self, now: float, stale_s: float) -> Tuple[bool, str]:
        if self.latest_minute_ms is None:
            return True, "no_station_data"
        if self.latest_adv_at is not None and now - self.latest_adv_at > stale_s:
            return True, f"no_new_minute_{int(now - self.latest_adv_at)}s"
        if now - self.latest_minute_ms / 1000.0 > stale_s + OFFICIAL_LAG_S:
            return True, f"newest_minute_{int(now - self.latest_minute_ms / 1000.0)}s_old"
        if self.short_history():
            return True, f"short_history_{int(self.history_span_s() / 60.0)}min"
        return False, ""

    def result(self, event: "EventInfo", now: float, stale_s: float) -> FairResult:
        try:
            snap = self.snapshot(now)
        except Exception as e:                           # noqa: BLE001
            self.errors += 1
            return FairResult({}, {}, True, f"wxtape_error:{type(e).__name__}")
        stale, why = self.stale(now, stale_s)
        strikes = [k for k, _ in event.markets]
        key = event.settle_ts
        hit = self._cache.get(key)
        if hit is not None and hit[0] is not snap:
            hit = None
        if hit is None:
            fr, sl, dist = {}, {}, None
            try:
                fr = self.model.fair(snap, event.settle_ts, strikes) or {}
                fn = getattr(self.model, "slopes", None)
                if callable(fn):
                    sl = fn(snap, event.settle_ts, strikes) or {}
                dist = self.model.dist(snap, event.settle_ts)
            except Exception:                            # noqa: BLE001
                self.errors += 1
            hit = (snap, fr, sl, dist)
            if len(self._cache) > 32:
                self._cache.clear()
            self._cache[key] = hit
        _snap, fr, sl, dist = hit
        fairs, slopes = {}, {}
        for k, t in event.markets:
            f = pol.clean_prob(_by_strike(fr, k))
            if f is not None:
                fairs[t] = f
                g = pol.clean_slope(_by_strike(sl, k))
                if g is not None:
                    slopes[t] = g
        return FairResult(fairs, slopes, stale, why, snap.latest_minute_ms,
                          getattr(snap, "est_latest", None) or snap.index_latest,
                          dist[0] if dist else None, dist[1] if dist else None)


def _by_strike(d: dict, k: float):
    v = d.get(k)
    if v is None:
        for kk, vv in d.items():
            try:
                if abs(float(kk) - k) < 1e-6:
                    return vv
            except (TypeError, ValueError):
                continue
    return v


# --------------------------------------------------------------------------
# State: events, orders, positions
# --------------------------------------------------------------------------

@dataclass
class EventInfo:
    event: str
    open_ts: float
    close_ts: float
    settle_ts: float
    markets: List[Tuple[float, str]]      # (strike, ticker), strikes ascending
    statuses: Dict[str, str] = field(default_factory=dict)
    settle_gap_s: float = 0.0             # API close_time - the ticker's hour (DST fold?)


def build_events(markets: Iterable[dict]) -> Dict[str, EventInfo]:
    by: Dict[str, List[dict]] = {}
    for m in markets or []:
        t = str(m.get("ticker") or "")
        if not t.startswith(SERIES + "-"):
            continue
        by.setdefault(m.get("event_ticker") or C.event_of(t), []).append(m)
    out: Dict[str, EventInfo] = {}
    for ev, ms in by.items():
        rows = []
        for m in ms:
            k = m.get("floor_strike")
            k = float(k) if k not in (None, "") else C.strike_of(m["ticker"])
            if k is None:
                continue
            rows.append((float(k), m["ticker"], m))
        if not rows:
            continue
        rows.sort()
        opens = [C.parse_iso(m.get("open_time") or "") for _, _, m in rows]
        closes = [C.parse_iso(m.get("close_time") or "") for _, _, m in rows]
        opens = [x for x in opens if x]
        closes = [x for x in closes if x]
        settle = event_settle_ts(ev)
        close = max(closes) if closes else settle
        if close is None:
            continue
        opn = min(opens) if opens else close - 3600.0
        gap = float(close) - float(settle) if (closes and settle is not None) else 0.0
        out[ev] = EventInfo(ev, float(opn), float(close), float(settle or close),
                            [(k, t) for k, t, _ in rows],
                            {t: str(m.get("status") or "") for _, t, m in rows}, gap)
    return out


@dataclass
class Order:
    """One of our resting orders as we believe it rests. total = Kalshi's
    max-fillable count (filled + resting); filled = the larger of the fills
    we booked for it and the fill count Kalshi last reported."""
    oid: str
    coid: str
    ticker: str
    side: str                 # 'yes' (YES bid at price) | 'no' (NO bid at price)
    price: int                # cents in its own side's space
    total: float
    placed_at: float
    expires_at: float
    tag: str = ""
    fills_seen: float = 0.0
    kfilled: float = 0.0
    confirmed: bool = False
    last_change: float = 0.0
    renewed: bool = False      # an expiry-only amend extended it (checked on reconcile)
    must_cancel: bool = False  # an urgent amend failed: the next decision cancels it

    @property
    def filled(self) -> float:
        return max(self.fills_seen, self.kfilled)

    @property
    def remaining(self) -> float:
        return max(0.0, self.total - self.filled)


@dataclass
class Op:
    kind: str                  # create | amend | decrease | cancel
    ticker: str
    side: str
    price: int                 # target price (create / amend), current (decrease / cancel)
    size: float                # create: count; amend: new resting size; decrease: reduce_by
    order: Optional[Order] = None
    urgent: bool = False
    reason: str = ""
    tag: str = ""
    renew: bool = False
    excess: float = 0.0        # urgent reprice: (price - guard cap) x size, the exposure


@dataclass
class MarketPos:
    yes_q: float = 0.0
    no_q: float = 0.0
    cost: float = 0.0
    fees: float = 0.0
    fills: int = 0


# --------------------------------------------------------------------------
# The order-level plan (pure)
# --------------------------------------------------------------------------

def tag_kind(tag: str) -> str:
    """'live' (live / near quotes), 'fav', 'pad' (1c pads and qualification
    pads) or '' (unknown)."""
    parts = {p for p in (tag or "").split("+") if p}
    if parts & {"live", "near"}:
        return "live"
    if "fav" in parts:
        return "fav"
    if parts and parts <= PASSIVE_TAGS:
        return "pad"
    return ""


def plan_side(ticker: str, side: str, orders: List[Order], want: Dict[int, float],
              tags: Dict[Tuple[str, int], str], now: float, cfg: Config,
              ext_y: Dict[int, float], ext_n: Dict[int, float],
              cap: Optional[int] = None, stable: bool = True) -> List[Op]:
    """Ops turning our resting orders on one side of one market into the
    policy's desired {price: size}. Same price: a cut is a decrease (the
    newest order first, a whole order cancelled), an increase a new order at
    the back of the queue (the sim's lot) or, inc_mode=amend, the newest
    order amended up. A price no longer wanted is paired with a new one and
    the order amended there in place (highest with highest), unless the new
    price would cross the external book (amend has no post_only): then
    cancel + a post_only create. Leftover old prices are cancelled, leftover
    new ones created. Untouched orders near expiry get an expiry-only amend.
    A same-price size change under max(size_tol_abs, size_tol_frac x size)
    is skipped (0 / 0 = exact, the sim's sizes). Urgent (never paced) = a
    cancel, a size cut, or a reprice down from a price above the fair
    guard's cap `cap` (None = unknown: every reprice down is urgent). Any
    other reprice is paced; a reprice UP (more aggressive) also waits until
    `stable` (the desired prices have held reprice_debounce_s): a reference
    that flickers 1c up and back costs no writes (a flicker down costs two:
    down at once, back up after the hold). A reprice that also
    cuts the size sends the cut at once (a decrease, or cancels of the other
    orders at the old price) and paces / debounces only the move. An order
    whose urgent amend failed (must_cancel) is cancelled."""
    ops: List[Op] = []
    by_p: Dict[int, List[Order]] = {}
    for o in sorted(orders, key=lambda o: (o.placed_at, o.oid)):
        if o.must_cancel and o.remaining > 0.5:
            ops.append(Op("cancel", ticker, side, o.price, o.remaining, o, True, "amend_failed",
                          o.tag))
            continue
        if o.remaining > 0.5:
            by_p.setdefault(o.price, []).append(o)
    want = {int(p): float(int(round(s))) for p, s in want.items() if s >= 1.0 - EPS}
    touched: Set[str] = {op.order.oid for op in ops if op.order is not None}
    for p in sorted(set(by_p) & set(want), reverse=True):
        lst = by_p[p]
        tot = sum(o.remaining for o in lst)
        w = want[p]
        tag = tags.get((side, p), "")
        if abs(w - tot) < max(cfg.size_tol_abs, cfg.size_tol_frac * tot, 0.5):
            continue
        if w < tot - 0.5:
            excess = tot - w
            for o in reversed(lst):
                if excess <= 0.5:
                    break
                touched.add(o.oid)
                if excess >= o.remaining - 0.5:
                    ops.append(Op("cancel", ticker, side, p, o.remaining, o, True, "size", o.tag))
                    excess -= o.remaining
                else:
                    ops.append(Op("decrease", ticker, side, p, float(int(round(excess))), o,
                                  True, "size", o.tag))
                    excess = 0.0
        elif w > tot + 0.5:
            delta = float(int(round(w - tot)))
            if cfg.inc_mode == "amend":
                o = lst[-1]
                touched.add(o.oid)
                ops.append(Op("amend", ticker, side, p, o.remaining + delta, o, False, "inc", tag))
            else:
                ops.append(Op("create", ticker, side, p, delta, None, False, "inc", tag))
    old = sorted(set(by_p) - set(want), reverse=True)
    new = sorted(set(want) - set(by_p), reverse=True)
    pairs: List[Tuple[int, int]] = []
    if cfg.amend_price:
        # an order moves only to a price of its own kind (a favorite never
        # "becomes" a 1c pad: that is a pull, cancelled at once)
        free = list(new)
        for po in old:
            k = tag_kind(by_p[po][0].tag)
            for pn in free:
                kn = tag_kind(tags.get((side, pn), ""))
                if k == kn or not k or not kn:
                    pairs.append((po, pn))
                    free.remove(pn)
                    break
        paired_old = {po for po, _ in pairs}
        old = [po for po in old if po not in paired_old]
        new = free
    for po, pn in pairs:
        lst = by_p[po]
        w = want[pn]
        tag = tags.get((side, pn), "")
        if pol.would_cross(ext_y, ext_n, side, pn):
            for o in lst:
                touched.add(o.oid)
                ops.append(Op("cancel", ticker, side, po, o.remaining, o, True, "reprice", o.tag))
            ops.append(Op("create", ticker, side, pn, w, None, False, "reprice", tag))
            continue
        keep = max(lst, key=lambda o: (o.remaining, -o.placed_at))
        tot = sum(o.remaining for o in lst)
        urgent = pn < po and (cap is None or po > cap)
        move = urgent or stable or pn < po          # only a move UP is debounced
        cut = (not urgent and
               w < tot - max(cfg.size_tol_abs, cfg.size_tol_frac * tot, 0.5))
        if not move:
            if cut:
                # the move waits (debounce); the size cut is risk: now, at the
                # old price, newest order first
                excess = tot - w
                for o in reversed(lst):
                    if excess <= 0.5:
                        break
                    touched.add(o.oid)
                    if excess >= o.remaining - 0.5:
                        ops.append(Op("cancel", ticker, side, po, o.remaining, o, True, "size",
                                      o.tag))
                        excess -= o.remaining
                    else:
                        ops.append(Op("decrease", ticker, side, po, float(int(round(excess))), o,
                                      True, "size", o.tag))
                        excess = 0.0
            continue
        for o in lst:
            if o is not keep:
                touched.add(o.oid)
                ops.append(Op("cancel", ticker, side, po, o.remaining, o, True, "reprice", o.tag))
        if cut and keep.remaining > w + 0.5:
            # the move is paced: its size cut goes first, at once
            ops.append(Op("decrease", ticker, side, po, float(int(round(keep.remaining - w))),
                          keep, True, "size", keep.tag))
        touched.add(keep.oid)
        exposure = ((po - cap) if cap is not None else 1.0) * keep.remaining if urgent else 0.0
        ops.append(Op("amend", ticker, side, pn, w, keep, urgent, "reprice", tag,
                      excess=exposure))
    for po in old:
        for o in by_p[po]:
            touched.add(o.oid)
            ops.append(Op("cancel", ticker, side, po, o.remaining, o, True, "pull", o.tag))
    for pn in new:
        ops.append(Op("create", ticker, side, pn, want[pn], None, False, "new",
                      tags.get((side, pn), "")))
    for lst in by_p.values():
        for o in lst:
            if o.oid in touched:
                continue
            left = o.expires_at - now
            if left < cfg.ttl_renew_s:
                ops.append(Op("amend", ticker, side, o.price, o.remaining, o,
                              left < cfg.ttl_urgent_s, "renew", o.tag, renew=True))
    return ops


def plan_market(ticker: str, orders: List[Order], want_y: Dict[int, float],
                want_n: Dict[int, float], tags: Dict[Tuple[str, int], str], now: float,
                cfg: Config, ext_y: Dict[int, float], ext_n: Dict[int, float],
                caps: Optional[Dict[str, Optional[int]]] = None,
                stable: Optional[Dict[str, bool]] = None) -> List[Op]:
    ops: List[Op] = []
    for side, want in (("yes", want_y), ("no", want_n)):
        ops += plan_side(ticker, side, [o for o in orders if o.side == side], want, tags,
                         now, cfg, ext_y, ext_n, (caps or {}).get(side),
                         (stable or {}).get(side, True))
    return ops


def guard_caps(P: dict, diag: Optional[dict]) -> Dict[str, Optional[int]]:
    """The fair guard's price cap per side (temp_mm_policy.quote_price:
    floor(100 x fair_side - buffer)), from a decision's diag; None when the
    decision priced without a model fair or the guard is off."""
    d = diag or {}
    fx, buf = d.get("fx"), d.get("buf")
    if fx is None or buf is None or d.get("fair_src") != "model" or not P.get("fair_guard"):
        return {"yes": None, "no": None}
    return {"yes": int(math.floor(100.0 * fx - buf + 1e-9)),
            "no": int(math.floor(100.0 * (1.0 - fx) - buf + 1e-9))}


# --------------------------------------------------------------------------
# Logging / email
# --------------------------------------------------------------------------

class Log:
    def __init__(self, log_dir: str, echo: bool = True, tag: str = "TMM"):
        self.dir = log_dir
        self.echo = echo
        self.tag = tag
        self._lock = threading.Lock()
        os.makedirs(log_dir, exist_ok=True)

    def __call__(self, msg: str, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        t = _dt.datetime.fromtimestamp(now, _dt.timezone.utc)
        line = f"{t.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}Z [{self.tag}] {msg}"
        with self._lock:
            if self.echo:
                try:
                    print(line, flush=True)
                except Exception:                        # noqa: BLE001
                    pass
            try:
                with open(os.path.join(self.dir, f"tmm_{t.strftime('%Y-%m-%d')}.log"),
                          "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def row(self, name: str, row: dict, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        rec = {"ts": round(now, 3), "at": C.iso(now), **row}
        with self._lock:
            try:
                with open(os.path.join(self.dir, f"{name}_{utc_day(now)}.jsonl"), "a",
                          encoding="utf-8") as f:
                    f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")
            except OSError:
                pass


def email_configured() -> bool:
    return bool(os.environ.get("ALERT_EMAIL_FROM")) and bool(os.environ.get("ALERT_EMAIL_PASSWORD"))


def send_email(subject: str, body: str, smtp: Callable[..., Any] = smtplib.SMTP_SSL) -> bool:
    """One alert through the bots' Gmail account (ALERT_EMAIL_*); recipient
    TMM_ALERT_TO, else ALERT_EMAIL_TO, else the sender. Never raises."""
    frm = os.environ.get("ALERT_EMAIL_FROM", "")
    pw = os.environ.get("ALERT_EMAIL_PASSWORD", "")
    to = os.environ.get("TMM_ALERT_TO") or os.environ.get("ALERT_EMAIL_TO") or frm
    if not (frm and pw and to):
        return False
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    try:
        with smtp("smtp.gmail.com", 465, timeout=15) as server:
            server.login(frm, pw)
            server.sendmail(frm, [r.strip() for r in to.split(",") if r.strip()],
                            msg.as_string())
        return True
    except Exception:                                    # noqa: BLE001
        return False


def write_json_atomic(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, default=str)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Reader thread: periodic REST reads off the trading thread
# --------------------------------------------------------------------------

class Reader:
    """Markets, programs, settlement results and (live) the account reads,
    on their own clocks; results go on a queue the trading thread drains.
    Writes never happen here."""

    def __init__(self, bot: "Bot"):
        self.bot = bot
        self.cfg = bot.cfg
        self.ex = bot.ex
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.last: Dict[str, float] = {}
        self.force: Set[str] = set()
        self.stop_evt = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.fills_cursor: Optional[float] = None
        self.settle_tried: Dict[str, float] = {}

    def due(self, name: str, every: float, now: float) -> bool:
        if name in self.force:
            self.force.discard(name)
            return True
        last = self.last.get(name)
        return last is None or now - last >= every

    def programs_every(self, now: float) -> float:
        if self.bot.reader_view.get("eager_programs"):
            return self.cfg.programs_eager_s
        minute = int((now % 3600.0) // 60.0)
        return self.cfg.programs_fast_s if minute <= 6 else self.cfg.programs_slow_s

    def _task(self, name: str, fn: Callable[[], None], now: float) -> None:
        self.last[name] = now
        try:
            fn()
        except Exception as e:                           # noqa: BLE001
            self.q.put(("error", name, f"{type(e).__name__}: {str(e)[:200]}", now))

    def _now(self, now: float) -> float:
        """A read's timestamp: when it started (threaded: the wall clock at
        the call, so a read forced after a suspicion is stamped after it)."""
        return max(now, time.time()) if self.bot.threaded else now

    def run_once(self, now: float) -> None:
        bot, cfg, ex = self.bot, self.cfg, self.ex
        if bot.halt_reason:
            return
        view = bot.reader_view
        if self.due("markets", cfg.markets_s, now):
            self._task("markets", lambda: self.q.put(("markets", ex.markets(now - 120.0), now)),
                       now)
        if self.due("programs", self.programs_every(now), now):
            self._task("programs", lambda: self.q.put(("programs", ex.programs(now), now)), now)
        for ev in view.get("need_results", []):
            if now - self.settle_tried.get(ev, 0.0) >= 120.0:
                self.settle_tried[ev] = now
                self._task("results", lambda ev=ev: self._results(ev, now), now)
        seed = getattr(bot.wx, "seed_rows", None)
        if view.get("wx_short") and callable(seed) and self.due("wx_seed", 120.0, now):
            before = view.get("wx_first_rx")
            self._task("wx_seed", lambda: self.q.put(("wx_seed", seed(self._now(now), before),
                                                      now)), now)
        if not bot.account_reads:
            return
        if self.due("balance", cfg.balance_s, now):
            self._task("balance", lambda: self.q.put(("balance", ex.balance(), now)), now)
        if not bot.live:
            return
        unconf = view.get("unconfirmed", [])
        if unconf and self.due("confirm", cfg.confirm_s, now):
            self._task("confirm", lambda: self._confirm(unconf, self._now(now)), now)
        if self.due("resting", cfg.orders_reconcile_s, now):
            self._task("resting", lambda: self._resting(self._now(now)), now)
        # fills first, then positions: a positions read is only compared
        # with fills booked before it
        verify = view.get("verify_events", [])
        verify_due = bool(verify) and self.due("verify", cfg.verify_s, now)
        if self.due("fills", cfg.fills_sweep_s, now) or verify_due:
            self._task("fills", lambda: self._fills(self._now(now)), now)
        if verify_due:
            self._task("verify", lambda: self._positions(verify, now), now)
        if self.due("positions", cfg.positions_s, now):
            evs = view.get("position_events", [])
            self._task("positions", lambda: self._positions(evs, now), now)

    def _resting(self, t0: float) -> None:
        self.q.put(("resting", self.ex.resting(), t0))

    def _positions(self, evs: List[str], now: float) -> None:
        for e in evs:
            t = self._now(now)
            self.q.put(("positions", e, self.ex.positions(e), t))

    def _results(self, ev: str, now: float) -> None:
        doc = C.read_json(os.path.join(C.DATA_ROOT, "settled", f"{ev}.json"), None)
        mk = (doc or {}).get("markets") if isinstance(doc, dict) else None
        if not mk:
            mk = self.ex.event_markets(ev)
        res = {}
        for m in mk or []:
            r = str(m.get("result") or "").lower()
            if r in ("yes", "no") and m.get("ticker"):
                res[m["ticker"]] = r
        self.q.put(("results", ev, res, now))

    def _confirm(self, unconf: List[Tuple[str, str, float]], now: float) -> None:
        """The batched check: the subaccount's own order list (GetOrders
        with subaccount=N) per event; an order missing from it for half the
        timeout is read back alone (GetOrder: its subaccount_number)."""
        events = sorted({e for _, e, _ in unconf})
        rows = []
        for i in range(0, len(events), 10):
            rows += self.ex.event_orders(events[i:i + 10])
        self.q.put(("confirm", rows, now))
        seen = {str(r.get("order_id")) for r in rows}
        for oid, _e, placed in unconf:
            if oid in seen or now - placed < self.cfg.confirm_timeout_s / 2.0:
                continue
            self.q.put(("readback", oid, self.ex.get_order(oid), now))

    def _fills(self, now: float) -> None:
        since = self.bot.fills_since
        if self.fills_cursor is not None:
            since = max(since, self.fills_cursor - 300.0)
        rows = self.ex.fills(since)
        self.fills_cursor = now
        self.q.put(("fills", rows, now))

    def start(self) -> None:
        def loop():
            while not self.stop_evt.is_set():
                try:
                    self.run_once(time.time())
                except Exception as e:                   # noqa: BLE001
                    self.q.put(("error", "reader", str(e)[:200], time.time()))
                self.stop_evt.wait(0.5)
        self.thread = threading.Thread(target=loop, daemon=True, name="tmm-reader")
        self.thread.start()

    def stop(self) -> None:
        self.stop_evt.set()


# --------------------------------------------------------------------------
# The bot
# --------------------------------------------------------------------------

class Bot:
    """The trading thread. step(now) is the whole decision cycle and takes
    the clock as an argument (the tests drive it with fake time, fake
    exchange, fake feed and fake station rows)."""

    def __init__(self, cfg: Config, P: dict, variant: str, live: bool, subaccount: int,
                 exchange: Any, feed: Any, wx: Any, fair: Any,
                 data_dir: str = DATA_DIR, clock: Callable[[], float] = time.time,
                 threaded: bool = True, mailer: Optional[Callable[[str, str], Any]] = None,
                 echo: bool = True, policy_overrides: Optional[dict] = None):
        if live and int(subaccount) <= 0:
            raise ValueError("live mode needs a numbered subaccount (N > 0)")
        self.cfg = cfg
        self.P = P
        self.P_stale = stale_policy(P)
        self.variant = variant
        self.policy_overrides = dict(policy_overrides or {})
        self.live = bool(live)
        self.sub = int(subaccount)
        self.ex = exchange
        self.feed = feed
        self.wx = wx
        self.fair = fair
        self.root_dir = data_dir
        self.dir = data_dir if self.live else os.path.join(data_dir, "dry")
        # the HALT this bot writes: the live one is KL-data\temp-mm\HALT, a dry
        # run's is dry\HALT (a dry experiment must never pause live trading);
        # both modes obey the root HALT (Jack's pause) and their own
        self.halt_path = os.path.join(self.dir, "HALT")
        self.halt_paths = sorted({os.path.join(data_dir, "HALT"), self.halt_path})
        self.clock = clock
        self.threaded = threaded
        self.mailer = mailer
        self.log = Log(self.dir, echo=echo, tag="TMM LIVE" if self.live else "TMM DRY")
        self.account_reads = self.live or self.sub > 0
        self.stats: CallStats = getattr(exchange, "stats", None) or CallStats()
        # state
        self.events: Dict[str, EventInfo] = {}
        self.programs: Dict[str, dict] = {}
        self.programs_at: Optional[float] = None
        self.orders: Dict[str, Order] = {}
        self.closed: Dict[str, float] = {}          # oid -> when we stopped tracking it
        self.unconfirmed: Dict[str, Tuple[str, float]] = {}
        self.first_confirmed = not self.live
        self.pos: Dict[str, MarketPos] = {}
        self.fill_keys: Set[str] = set()
        self.results: Dict[str, str] = {}
        self.marks: Dict[str, float] = {}
        self.halted_events: Dict[str, str] = {}
        self.pos_mismatch: Counter = Counter()
        self.halt_reason: Optional[str] = None
        self.halt_file_seen = False
        self.cash: Optional[float] = None
        self.cash_at: Optional[float] = None
        self.cash_spent = 0.0
        self.amend_expiry_ok = True
        self.fills_since = 0.0
        # write health: a 429 / 409 (paused) / 5xx / network failure stops
        # every write until write_backoff_until (0.5 s doubling to 8 s, reset
        # by a success); never counted as an order error
        self.write_backoff_until = 0.0
        self.backoff_s = 0.0
        self.unhealthy_since: Optional[float] = None
        self.unhealthy_mailed = False
        self.backoff_counts: Counter = Counter()
        self.op_cool: Dict[Tuple[str, str], float] = {}
        # fills we may have missed: ticker -> since. New orders there are held
        # until a fills sweep and a positions read taken after `since` agree;
        # a positions mismatch seen again after a fills sweep halts the bot
        self.suspect: Dict[str, float] = {}
        self.mismatch_at: Dict[str, float] = {}
        self.last_fills_read = -1e18
        self.traded: Dict[str, float] = {}           # event -> last live create
        self.cancelled_ids: Dict[str, float] = {}    # oid -> when Kalshi said cancelled / gone
        self.ambiguous: Dict[str, dict] = {}         # coid -> a create with no answer
        self.last_resting_read = -1e18
        self.confirm_misses: Counter = Counter()
        self.unconf_ticker: Dict[str, str] = {}
        self.confirm_hold = False
        self.feed_ok_prev: Optional[bool] = None
        self.feed_connects: Optional[int] = None
        self.mailed_notes: Set[str] = set()
        self.settle_logged: Set[str] = set()
        self.hours_logged: Set[str] = set()       # events logged as outside quote_hours_et
        # pacing
        t0 = clock()
        self.wbucket = Bucket(cfg.write_rate, cfg.write_burst, t0)
        self.new_orders: Deque[float] = deque()
        self.writes: Deque[float] = deque()
        self.last_act: Dict[str, float] = {}
        self.want_since: Dict[Tuple[str, str], Tuple[tuple, float]] = {}
        self.errors: Deque[float] = deque()
        self.err_mail_at = 0.0
        self.cooled: Dict[Tuple[str, str, int], float] = {}
        # bookkeeping
        self.started = t0
        self.last_decide = 0.0
        self.last_wflag: Optional[bool] = None
        self.need_decide = True
        self.steps = 0
        self.decides = 0
        self.lat: Deque[Tuple[float, float]] = deque()
        self.react: Deque[float] = deque(maxlen=600)
        self.guard_counts: Counter = Counter()
        self.guards_now: Dict[str, Any] = {}
        self.op_counts: Counter = Counter()
        self.deferred: Counter = Counter()
        self.last_sample: Dict[str, float] = {}
        self.wflag_prev: Dict[str, bool] = {}
        self.pad_hold_prev: Dict[str, bool] = {}     # event -> pad hold (pad_hold_open_s) last step
        self.pulls: Deque[dict] = deque(maxlen=30)
        self.last_status = 0.0
        self.last_save = 0.0
        self.last_minute_log = 0.0
        self.et_day = C.et_day(t0)
        self.day_counts: Counter = Counter()
        self.reader_view: Dict[str, Any] = {}
        self.wx_seen_at: Optional[float] = None
        self.stopped = False
        self.stop_evt = threading.Event()
        self.wake = threading.Event()
        self.reader = Reader(self)
        self.fair_last: Dict[str, FairResult] = {}
        self.resting_wcl: Dict[str, float] = {}
        self.state_path = os.path.join(self.dir, f"state{'_sub' + str(self.sub) if self.live else ''}.json")

    # ---- small utilities -------------------------------------------------
    def _mail(self, subject: str, body: str) -> None:
        if self.mailer is None or not self.live:
            return
        mailer = self.mailer

        def run():
            if mailer(subject, body) is False:
                self.log(f"! alert email failed: {subject}")
        if self.threaded:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()

    def _event_of(self, t: str) -> str:
        return C.event_of(t)

    def _orders_of(self, t: str) -> List[Order]:
        return [o for o in self.orders.values() if o.ticker == t]

    def own_levels(self, t: str) -> Tuple[Dict[int, float], Dict[int, float]]:
        y: Dict[int, float] = {}
        n: Dict[int, float] = {}
        for o in self.orders.values():
            if o.ticker != t or o.remaining <= EPS:
                continue
            d = y if o.side == "yes" else n
            d[o.price] = d.get(o.price, 0.0) + o.remaining
        return y, n

    def _forget(self, o: Order, now: float) -> None:
        self.orders.pop(o.oid, None)
        self.closed[o.oid] = now

    def _olog(self, kind: str, now: float, **row) -> None:
        self.log.row("orders", {"kind": kind, "mode": "live" if self.live else "dry",
                                "variant": self.variant, **row}, now)

    def _backoff(self, now: float, cls: str, why: str) -> None:
        """A transient write failure (429 / 409 paused / 5xx / network): no
        writes for backoff_s (0.5 s doubling to backoff_max_s). Failed ops
        stay planned (their orders stay tracked) and go again after it."""
        cfg = self.cfg
        self.backoff_s = min(cfg.backoff_max_s, max(cfg.backoff_min_s, self.backoff_s * 2.0))
        self.write_backoff_until = max(self.write_backoff_until, now + self.backoff_s)
        self.backoff_counts[cls] += 1
        if self.unhealthy_since is None:
            self.unhealthy_since = now
            self.log(f"! writes failing ({cls}: {why[:200]}): backing off {self.backoff_s:g} s, "
                     f"doubling to {cfg.backoff_max_s:g} s; what rests stays until its TTL", now)

    def _write_ok(self, now: float) -> None:
        if self.unhealthy_since is not None:
            self.log(f"writes recovered after {now - self.unhealthy_since:.1f} s "
                     f"({dict(self.backoff_counts)})", now)
            self.unhealthy_since = None
            self.unhealthy_mailed = False
        self.backoff_s = 0.0

    def _note_mail(self, key: str, subject: str, body: str) -> None:
        """One email per key for the life of the process."""
        if key in self.mailed_notes:
            return
        self.mailed_notes.add(key)
        self._mail(subject, body)

    def _order_error(self, now: float, what: str, e: ApiError, counted: bool = True) -> None:
        if not counted:
            return
        self.errors.append(now)
        while self.errors and self.errors[0] < now - ERR_WINDOW_S:
            self.errors.popleft()
        n = len(self.errors)
        if n >= self.cfg.order_error_halt:
            self.halt(now, f"{n} order errors in {int(ERR_WINDOW_S)} s (last: {what}: {e} "
                           f"{e.body[:200]})")
        elif n >= self.cfg.order_error_alert and now - self.err_mail_at > 1800.0:
            self.err_mail_at = now
            self._mail(f"Temp MM: {n} order errors in 5 min",
                       f"Last: {what}: {e} {e.body[:300]}\n\nThe bot keeps running; it halts at "
                       f"{self.cfg.order_error_halt}. Logs: {self.dir}")

    # ---- startup / shutdown ----------------------------------------------
    def startup(self, now: float) -> int:
        cfg = self.cfg
        self.log(f"start: {'LIVE' if self.live else 'DRY RUN'}, variant {self.variant}, "
                 f"subaccount {self.sub}, policy overrides {self.policy_overrides or '{}'}, "
                 f"q_live {self.P['q_live']}, decided {self.P['decided']}, skew {self.P['skew']}, "
                 f"pos_cap {self.P['pos_cap']}, event_wcl_cap {self.P['event_wcl_cap']}, "
                 f"quote hours (ET settle) {self.P.get('quote_hours_et') or 'all'}, "
                 f"loss halts ${cfg.event_loss_halt:g} event / ${cfg.daily_loss_halt:g} day, "
                 f"TTL {cfg.order_ttl_s:g}s (pads {cfg.pad_ttl_s:g}s), data {self.dir}"
                 + (f", orders via {getattr(self.ex, 'api_base', '?')}" if self.live else ""), now)
        ps = getattr(self.fair, "params_source", None)
        cs = getattr(self.fair, "cals_source", None)
        if ps is not None or cs is not None:
            self.log(f"fair model params: {ps}; calibrations: {cs}", now)
            if "defaults" in (ps, cs):
                why = (f"the fair model would run on built-in defaults (params: {ps}; calibrations: "
                       f"{cs}), not the model the shadow validated. Restore "
                       f"{C.MODEL_PARAMS_FILE} (and the calibrations), or point TMM_MODEL_PARAMS / "
                       f"TMM_CALIBRATIONS at copies")
                if self.live:
                    return self.refuse(now, why)
                self.log(f"! {why}", now)
        self._load_state(now)
        self.fills_since = C.et_day_bounds(C.et_day(now))[0] - 3600.0
        self._restore_fills(now)
        if self.account_reads:
            try:
                self.cash = self.ex.balance()
                self.cash_at, self.cash_spent = now, 0.0
            except Exception as e:                       # noqa: BLE001
                if self.live:
                    return self.refuse(now, f"cannot read subaccount {self.sub}'s balance: {e}")
                self.log(f"! balance read failed: {e}", now)
            if self.live and (not self.cash or self.cash <= 0):
                return self.refuse(now, f"subaccount {self.sub} holds ${self.cash or 0:.2f} -- "
                                        f"fund it first: python kalshi_subaccount.py fund "
                                        f"{self.sub} <dollars>")
            self.log(f"subaccount {self.sub} cash ${self.cash or 0:,.2f}", now)
        if self.live:
            self._cancel_strays_sync(now, "startup")
            try:
                rows = self.ex.fills(self.fills_since)
                n = sum(1 for f in rows if self._on_fill(f, "rest", now))
                self.reader.last["fills"] = now
                self.reader.fills_cursor = now
                self.log(f"fills since {C.iso(self.fills_since)}: {len(rows)} read, {n} new", now)
            except Exception as e:                       # noqa: BLE001
                self.log(f"! startup fills read failed: {e}", now)
        try:
            rows = self.wx.start(now)
            self.fair.add(rows, now)
            note = getattr(self.wx, "seed_note", "")
            self.log(f"wx history: {len(rows)} rows" + (f" ({note})" if note else
                                                        " from the collector's stream"), now)
        except Exception as e:                           # noqa: BLE001
            self.log(f"! wx history failed: {e}", now)
        # the first discovery and programs pass in line, so the first
        # decision can quote
        self.reader.run_once(now)
        self._drain_reader(now)
        self.feed.set_markets(self._feed_tickers(now))
        if self.threaded:
            self.feed.start()
            self.reader.start()
        return EXIT_OK

    def refuse(self, now: float, why: str) -> int:
        self.log(f"REFUSED to start: {why}", now)
        self._mail("Temp MM refused to start", why)
        return EXIT_REFUSED

    def shutdown(self, now: float, why: str) -> None:
        if self.stopped:
            return
        self.stopped = True
        self.log(f"shutting down ({why}): cancelling every tmm- order", now)
        try:
            self.cancel_all(now, f"shutdown:{why}", sweep=True)
        except Exception as e:                           # noqa: BLE001
            self.log(f"! shutdown cancel failed: {e}; the order TTL is the backstop", now)
        for comp in (self.reader, self.wx, self.feed):
            try:
                comp.stop()
            except Exception:                            # noqa: BLE001
                pass
        self._save_state(now)
        self._status(now, state="stopped")

    def halt(self, now: float, why: str, exit_bot: bool = True,
             misbooked: Optional[List[Tuple[str, str, int]]] = None,
             probe: Optional[List[Tuple[str, str]]] = None) -> None:
        """Bot-level halt: cancel all, write HALT, email; the run loop exits 3.
        misbooked: (oid, ticker, subaccount) of orders Kalshi booked outside
        our subaccount -- cancelled THERE and read back. probe: (oid, ticker)
        of orders whose subaccount is unknown -- after the cancel-all, also
        cancelled in the primary and read back. The email lists any order
        not confirmed cancelled."""
        if self.halt_reason:
            return
        self.halt_reason = why
        self.log(f"!! HALT: {why}", now)
        problems: List[str] = []
        notes: List[str] = []
        for oid, t, sa in misbooked or []:
            try:
                ok, msg = self._cancel_elsewhere(now, oid, t, sa)
            except Exception as e:                       # noqa: BLE001
                ok, msg = False, (f"order {oid} ({t}), booked to subaccount {sa}: cancel failed "
                                  f"({type(e).__name__}: {e}) -- cancel it by hand")
            (notes if ok else problems).append(msg)
        left: Optional[Set[str]] = None
        try:
            left = self.cancel_all(now, "halt", sweep=True)
        except Exception as e:                           # noqa: BLE001
            self.log(f"! halt cancel failed: {e}", now)
        for oid, t in probe or []:
            try:
                ok, msg = self._cancel_elsewhere(now, oid, t, 0)
            except Exception as e:                       # noqa: BLE001
                ok, msg = False, f"order {oid} ({t}): read-back failed ({e}) -- check it by hand"
            (notes if ok else problems).append(msg)
        if left is not None and probe:
            left -= {oid for oid, _ in probe}
        if left is None:
            problems.append("the cancel-all failed: what rests expires by its TTL (90 s, pads 300 s)")
        elif left:
            problems.append(f"{len(left)} tracked order(s) could not be cancelled "
                            f"({', '.join(sorted(left)[:12])}): they expire by their TTL "
                            f"(90 s, pads 300 s)")
        try:
            with open(self.halt_path, "w", encoding="utf-8") as f:
                f.write(f"{C.iso(now)} {why}\n")
        except OSError:
            pass
        if problems:
            done = "NOT everything was cancelled:\n" + "\n".join(f"- {p}" for p in problems)
        else:
            done = f"Every tmm- order in subaccount {self.sub} was cancelled"
        if notes:
            done += "\n" + "\n".join(f"- {n}" for n in notes)
        self._mail(f"Temp MM HALTED: {why[:80]}",
                   f"{why}\n\n{done}\n\n{self.halt_path} written; the bot exited (code 3) and the "
                   f"launcher stops. To resume: check the logs in {self.dir}, delete HALT, start "
                   f"it again.")
        self._status(now, state="halted")

    def _cancel_elsewhere(self, now: float, oid: str, ticker: str, sa: int) -> Tuple[bool, str]:
        """Cancel an order of ours where Kalshi booked it (subaccount sa; 0 =
        the primary), then read it back. (ok, message)."""
        o = self.orders.get(oid)
        if o is not None:
            self._forget(o, now)            # cancel_all's ?subaccount=N cannot reach it
        if not self.live:
            return True, ""
        detail = ""
        try:
            self.ex.cancel(oid, ticker, subaccount=int(sa))
            detail = "cancel accepted"
        except ApiError as e:
            detail = f"cancel: {e} {e.body[:100]}"
        status = None
        try:
            rec = self.ex.get_order(oid, sync=True)
            status = str((rec or {}).get("status") or "").lower() or None
            detail += f"; read-back status {status}"
        except Exception as e:                           # noqa: BLE001
            detail += f"; read-back failed: {e}"
        ok = status in ("canceled", "cancelled", "executed", "expired")
        msg = (f"order {oid} ({ticker}), booked to subaccount {sa}: "
               + ("cancelled (read back)" if ok else
                  f"NOT confirmed cancelled ({detail}) -- cancel it by hand on kalshi.com or with "
                  f"DELETE /portfolio/events/orders/{oid}" + (f"?subaccount={sa}" if sa else "")))
        self.log(("" if ok else "! ") + msg, now)
        self._olog("cancel_elsewhere", now, oid=oid, ticker=ticker, subaccount=sa, ok=ok,
                   detail=detail)
        return ok, msg

    # ---- the cycle -------------------------------------------------------
    def step(self, now: float) -> None:
        t0 = time.perf_counter()
        marks = [t0]
        self.steps += 1
        if not self.threaded:
            self.reader.run_once(now)
        # the feed first: a WebSocket fill is booked before a positions read
        # drained in the same step is compared with our fills
        self._drain_feed(now)
        self._drain_reader(now)
        self._check_feed(now)
        marks.append(time.perf_counter())
        self._ingest_wx(now)
        marks.append(time.perf_counter())
        self._roll_day(now)
        if self.halt_reason:
            return
        self._check_confirm_timeouts(now)
        if self.halt_reason:
            return
        hp = next((p for p in self.halt_paths if os.path.exists(p)), None)
        if hp:
            if not self.halt_file_seen:
                self.halt_file_seen = True
                self.log(f"HALT file present ({hp}): cancelling all, idling", now)
                left = self.cancel_all(now, "halt_file", sweep=True)
                self._mail("Temp MM paused: HALT file present",
                           f"{hp} is present (written by Jack, --cancel-all, or an earlier halt). "
                           f"The bot cancelled every tmm- order"
                           + (f" except {len(left)} it could not ({', '.join(sorted(left)[:8])}; "
                              f"they expire by their TTL)" if left else "")
                           + f" and idles; it quotes again once the file is deleted.")
            elif self.orders:
                self.cancel_all(now, "halt_file")
            self._housekeeping(now, state="halt_file")
            return
        if self.halt_file_seen:
            self.halt_file_seen = False
            self.log("HALT file gone: quoting resumes", now)
            self.need_decide = True
        self._expire(now)
        # the landing window's edges are decided on the first wake past them
        # (<= wake_s late), not the next loop_s tick
        lat = float(self.P["order_latency_s"])
        wf = pol.window_flag(self.P, now + lat, now + lat + self.cfg.pull_lead_s)
        if wf != self.last_wflag:
            self.need_decide = True
        # so is the end of an event's pad hold: the 1c pads go up on that wake
        if self._pad_hold_edges(now, lat):
            self.need_decide = True
        if self.need_decide or now - self.last_decide >= self.cfg.loop_s:
            self.need_decide = False
            self.last_decide = now
            self.last_wflag = wf
            self.decides += 1
            ops: List[Op] = []
            flips: List[Tuple[str, bool, int]] = []
            for ev in self._quotable_events(now):
                ops += self._event_ops(ev, now, flips)
            marks.append(time.perf_counter())
            self._execute(ops, now)
            marks.append(time.perf_counter())
            for ev_name, flag, n in flips:
                rec = {"kind": "pull" if flag else "restore", "event": ev_name,
                       "second": round(now % 60.0, 3), "orders": n,
                       "done_s": round(time.perf_counter() - t0, 4)}
                self.pulls.append(rec)
                self.log.row("decisions", rec, now)
            if self.wx_seen_at is not None:
                self.react.append(round(time.perf_counter() - t0 + (now - self.wx_seen_at), 4))
                self.wx_seen_at = None
            self._check_losses(now)
            self._prune_events(now)
            self._update_reader_view(now)
        dt = time.perf_counter() - t0
        self.lat.append((now, dt))
        if dt > 0.2:
            parts = [round((b - a) * 1000, 1) for a, b in zip(marks, marks[1:])]
            self.log(f"slow step {dt * 1000:.0f} ms (inputs, wx, decide, execute ms: {parts})", now)
        self._housekeeping(now)

    def run(self, duration: Optional[float] = None, until: Optional[float] = None) -> int:
        start = self.clock()
        code = self.startup(start)
        if code != EXIT_OK:
            self.stopped = True
            return code
        try:
            while not self.stop_evt.is_set():
                now = self.clock()
                if self.halt_reason:
                    return EXIT_HALTED
                if duration is not None and now - start >= duration:
                    self.log(f"--duration {duration:g}s reached", now)
                    return EXIT_OK
                if until is not None and now >= until:
                    self.log("--until reached", now)
                    return EXIT_OK
                self.wake.wait(self.cfg.wake_s)
                self.wake.clear()
                try:
                    self.step(self.clock())
                except Exception as e:                   # noqa: BLE001
                    self.log(f"! step failed: {type(e).__name__}: {str(e)[:300]}")
                    self._order_error(self.clock(), "step", _api_error(e))
            return EXIT_HALTED if self.halt_reason else EXIT_OK
        except KeyboardInterrupt:
            self.log("Ctrl+C")
            return EXIT_OK
        finally:
            self.shutdown(self.clock(), self.halt_reason or "exit")

    # ---- inputs ----------------------------------------------------------
    def _drain_feed(self, now: float) -> None:
        try:
            dirty, events, dropped = self.feed.drain()
        except Exception:                                # noqa: BLE001
            return
        if dirty or dropped:
            self.need_decide = True
        for kind, body in events:
            if kind == "fill" and self._on_fill(body, "ws", now):
                self.need_decide = True

    def _check_feed(self, now: float) -> None:
        """A WebSocket that went unhealthy (or reconnected) may have dropped
        fills: every market of an event we traded turns suspect -- a fills
        sweep and a positions read before new orders there (live)."""
        ok = bool(self.feed.healthy())
        conn = None
        try:
            conn = (self.feed.status() or {}).get("connects")
        except Exception:                                # noqa: BLE001
            pass
        if self.live:
            back = ((ok and self.feed_ok_prev is False)
                    or (conn is not None and self.feed_connects is not None
                        and conn != self.feed_connects))
            if back:
                n = 0
                for ev in list(self.traded):
                    info = self.events.get(ev)
                    if info is None or info.close_ts < now - 60.0:
                        continue
                    for _, t in info.markets:
                        n += t not in self.suspect
                        self._suspect(now, t, "", quiet=True)
                if n:
                    self.log(f"WebSocket back (connects {conn}): fills may have been missed; "
                             f"{n} market(s) held until a fills sweep and positions agree", now)
                    self.reader.force.add("fills")
        self.feed_ok_prev = ok
        if conn is not None:
            self.feed_connects = conn

    def _ingest_wx(self, now: float) -> None:
        try:
            rows = self.wx.poll(now)
        except Exception as e:                           # noqa: BLE001
            self.guard_counts["wx_error"] += 1
            rows = []
            self.log(f"! wx poll failed: {e}", now)
        if rows:
            self.fair.add(rows, now)
            self.need_decide = True
            if self.wx_seen_at is None:
                self.wx_seen_at = now
        note = self.fair.maybe_reload(now) if hasattr(self.fair, "maybe_reload") else None
        if note:
            self.log(note, now)
            if note.startswith("!"):
                self._note_mail("reload:" + note[:60], "Temp MM: fair model reload problem",
                                f"{note}\n\nThe bot keeps quoting on the model it loaded at "
                                f"start ({getattr(self.fair, 'params_source', '?')}).")

    def _drain_reader(self, now: float) -> None:
        while True:
            try:
                item = self.reader.q.get_nowait()
            except queue.Empty:
                break
            try:
                self._on_reader(item, now)
            except Exception as e:                       # noqa: BLE001
                self.log(f"! reader result {item[0]} failed: {type(e).__name__}: {e}", now)
        self._update_reader_view(now)

    def _on_reader(self, item: tuple, now: float) -> None:
        kind = item[0]
        if kind == "markets":
            evs = build_events(item[1])
            new = [e for e in evs if e not in self.events]
            for e, info in evs.items():
                self.events[e] = info
            for e in [e for e, info in self.events.items() if info.close_ts < now - 7200.0]:
                self.events.pop(e, None)
            if new:
                self.log(f"events: {', '.join(sorted(new))} "
                         f"({sum(len(evs[e].markets) for e in new)} markets)", now)
                self.reader.force.add("programs")
            self.feed.set_markets(self._feed_tickers(now))
            self.need_decide = True
        elif kind == "programs":
            progs = {}
            for p in item[1]:
                if str(p.get("incentive_type") or "liquidity") not in ("liquidity", ""):
                    continue
                pp = program_params(p)
                if pp is None:
                    continue
                t = p.get("market_ticker")
                cur = progs.get(t)
                if cur is None or pp["end"] > cur["end"]:
                    progs[t] = pp
            if set(progs) != set(self.programs):
                self.log(f"programs: {len(progs)} KXTEMPMIAH liquidity programs", now)
            self.programs = progs
            self.programs_at = item[2]
            self.need_decide = True
        elif kind == "balance":
            if item[1] is not None:
                self.cash, self.cash_at, self.cash_spent = float(item[1]), item[2], 0.0
        elif kind == "results":
            ev, res = item[1], item[2]
            if res:
                self.results.update(res)
                self.log(f"results {ev}: {sum(1 for r in res.values() if r == 'yes')} yes / "
                         f"{sum(1 for r in res.values() if r == 'no')} no; event P&L "
                         f"${self.event_pnl(ev):+.2f}", now)
                self._save_state(now)           # a restart must not lose them
        elif kind == "wx_seed":
            rows = item[1] or []
            if rows:
                self.fair.add(rows, now)
                self.need_decide = True
            self.log(f"wx seed: {len(rows)} history rows ({getattr(self.wx, 'seed_note', '')})", now)
        elif kind == "resting":
            self._reconcile_resting(item[1], item[2], now)
        elif kind == "fills":
            n = sum(1 for f in item[1] if self._on_fill(f, "rest", now))
            self.last_fills_read = max(self.last_fills_read, float(item[2]))
            if n:
                self.log(f"fills sweep: {n} fill(s) the WebSocket had not delivered", now)
                self.need_decide = True
        elif kind == "positions":
            self._check_positions(item[1], item[2], item[3] if len(item) > 3 else now, now)
        elif kind == "confirm":
            self._on_confirm(item[1], now)
        elif kind == "readback":
            self._on_readback(item[1], item[2], now)
        elif kind == "error":
            self.guard_counts[f"read_error_{item[1]}"] += 1
            self.log(f"! read {item[1]}: {item[2]}", now)

    def _update_reader_view(self, now: float) -> None:
        # settlement results for every event we hold, from the positions --
        # not self.events, which a restart rebuilds without closed events
        held: Dict[str, List[str]] = {}
        for t, mp in self.pos.items():
            if mp.yes_q or mp.no_q or mp.fills:
                held.setdefault(self._event_of(t), []).append(t)
        need = []
        for ev, ticks in held.items():
            info = self.events.get(ev)
            close = info.close_ts if info is not None else event_settle_ts(ev)
            if close is None or now - close < 360.0:
                continue
            if not all(t in self.results for t in ticks):
                need.append(ev)
        # positions: every event we placed orders in (or hold), until its close
        pos_evs = set(self.traded) | {self._event_of(t) for t, mp in self.pos.items()
                                      if mp.yes_q or mp.no_q}
        pos_evs = sorted(e for e in pos_evs
                         if e in self.events and self.events[e].close_ts > now - 60.0)
        for t in list(self.suspect):
            info = self.events.get(self._event_of(t))
            if info is None or info.close_ts < now - 60.0:
                self.suspect.pop(t, None)        # nothing more is quoted there
        eager = any(info.open_ts <= now < info.open_ts + 300.0 and info.close_ts > now
                    and not any(t in self.programs for _, t in info.markets)
                    for info in self.events.values())
        short = getattr(self.fair, "short_history", None)
        first_rx = getattr(self.fair, "first_rx", None)
        self.reader_view = {
            "unconfirmed": [(oid, e, placed) for oid, (e, placed) in self.unconfirmed.items()],
            "need_results": need, "position_events": pos_evs, "eager_programs": eager,
            "verify_events": sorted({self._event_of(t) for t in self.suspect}),
            "wx_short": bool(short()) if callable(short) else False,
            "wx_first_rx": first_rx() if callable(first_rx) else None}

    def _prune_events(self, now: float) -> None:
        """Per-event state of events no longer quotable (the once-a-minute
        line printed every closed event forever)."""
        q = {e.event for e in self._quotable_events(now)}
        for d in (self.guards_now, self.wflag_prev, self.last_sample, self.fair_last,
                  self.resting_wcl, self.pad_hold_prev):
            for k in [k for k in d if k not in q]:
                d.pop(k, None)
        self.hours_logged &= q
        for e in [e for e, ts in self.traded.items()
                  if (self.events.get(e).close_ts if e in self.events else ts) < now - 3600.0]:
            self.traded.pop(e, None)

    def _pad_hold_edges(self, now: float, lat: float) -> bool:
        """True when an event's pad hold (temp_mm_policy.pad_hold_flag: no 1c
        pads while the orders would start resting before the open +
        pad_hold_open_s) ended since the last step, so the bot decides on this
        wake (<= wake_s late) instead of the next loop_s tick -- the test the
        policy applies, on the same resting start (now + order latency).
        Logged once per event."""
        hold_s = float(self.P.get("pad_hold_open_s") or 0.0)
        if hold_s <= 0.0:
            return False
        edge = False
        for ev in self._quotable_events(now):
            h = pol.pad_hold_flag(self.P, now + lat, ev.open_ts)
            prev = self.pad_hold_prev.get(ev.event)
            self.pad_hold_prev[ev.event] = h
            if prev and not h:
                edge = True
                self.log(f"{ev.event}: pad hold over ({hold_s:g} s after the open "
                         f"{C.iso(ev.open_ts)}): 1c pads from now", now)
                self.log.row("decisions", {"kind": "pad_hold_end", "event": ev.event,
                                           "after_open_s": round(now + lat - ev.open_ts, 3)},
                             now)
        return edge

    def _feed_tickers(self, now: float) -> List[str]:
        out = []
        for info in self.events.values():
            if info.close_ts > now - 60.0 and info.open_ts < now + 900.0:
                out += [t for _, t in info.markets]
        return sorted(out)

    def _quotable_events(self, now: float) -> List[EventInfo]:
        return sorted((info for info in self.events.values()
                       if info.close_ts > now - 5.0 and info.open_ts < now + 900.0),
                      key=lambda e: e.close_ts)

    # ---- fills / positions ----------------------------------------------
    def _on_fill(self, f: dict, src: str, now: float) -> bool:
        """Book one fill (WS message body or REST fill), once (trade_id +
        order_id). Returns True when it was new."""
        t = str(f.get("market_ticker") or f.get("ticker") or "")
        if not t.startswith(SERIES + "-"):
            return False
        oid = str(f.get("order_id") or "")
        coid = str(f.get("client_order_id") or "")
        o = self.orders.get(oid)
        ours = (o is not None or oid in self.closed or oid in self.unconfirmed
                or coid.startswith(CLIENT_PREFIX + "-") or src in ("rest", "log"))
        if not ours:
            return False
        key = f"{f.get('trade_id') or f.get('fill_id') or ''}:{oid}"
        if key in self.fill_keys:
            return False
        n = fpf(f.get("count_fp"), fpf(f.get("count")))
        if n <= 0:
            return False
        sa = f.get("subaccount", f.get("subaccount_number"))
        if self.live and src == "ws" and sa is not None and int(sa) != self.sub:
            self.halt(now, f"fill on {t} (order {oid}) was booked to subaccount {sa}, "
                           f"not {self.sub}", misbooked=[(oid, t, int(sa))])
            return False
        self.fill_keys.add(key)
        if o is not None:
            side = o.side
        else:
            side = str(f.get("side_ours") or f.get("purchased_side") or f.get("outcome_side")
                       or ("yes" if f.get("book_side") == "bid" else "no"))
        # the price the fill executed at (an amend may have moved the order
        # since); the order's price only when the fill carries none
        if f.get("price_ours") is not None:
            price = int(f["price_ours"])
        else:
            fp = fill_price_cents(f, side)
            price = fp if fp is not None else (o.price if o is not None else 0)
        fee = fpf(f.get("fee_cost"))
        mp = self.pos.setdefault(t, MarketPos())
        if side == "yes":
            mp.yes_q += n
        else:
            mp.no_q += n
        mp.cost += n * price / 100.0
        mp.fees += fee
        mp.fills += 1
        self.cash_spent += n * price / 100.0 + fee
        self.day_counts["fills"] += 1
        self.day_counts["contracts"] += n
        if o is not None:
            o.fills_seen += n
            if o.remaining <= 0.5:
                self._forget(o, now)
        if src != "log":
            self.log.row("fills", {"src": src, "ticker": t, "event": self._event_of(t),
                                   "side_ours": side, "price_ours": price, "count": n,
                                   "fee": fee, "order_id": oid, "client_order_id": coid,
                                   "trade_id": f.get("trade_id"), "is_taker": f.get("is_taker"),
                                   "subaccount": sa, "pos_yes": mp.yes_q, "pos_no": mp.no_q,
                                   "mode": "live" if self.live else "dry"}, now)
            self.log(f"FILL {t} {side.upper()} {n:g} @ {price}c ({src}) -> "
                     f"pos {mp.yes_q:g} YES / {mp.no_q:g} NO", now)
        return True

    def _restore_fills(self, now: float) -> None:
        """Today's and yesterday's fills logs (this mode's dir): positions and
        the day's P&L survive a restart."""
        n = 0
        for d in (utc_day(now - 86400.0), utc_day(now)):
            for r in C.iter_jsonl(os.path.join(self.dir, f"fills_{d}.jsonl")):
                ts = fpf(r.get("ts"))
                if ts < self.fills_since:
                    continue
                f = {"market_ticker": r.get("ticker"), "order_id": r.get("order_id"),
                     "trade_id": r.get("trade_id"), "count_fp": r.get("count"),
                     "fee_cost": r.get("fee"), "side_ours": r.get("side_ours"),
                     "price_ours": r.get("price_ours")}
                if self._on_fill(f, "log", now):
                    n += 1
        if n:
            self.log(f"restored {n} fills from the logs", now)

    def mark(self, t: str) -> Optional[float]:
        r = self.results.get(t)
        if r == "yes":
            return 1.0
        if r == "no":
            return 0.0
        return self.marks.get(t)

    def event_pnl(self, ev: str) -> float:
        tot = 0.0
        for t, mp in self.pos.items():
            if self._event_of(t) != ev:
                continue
            y = self.mark(t)
            if y is None:
                tot -= mp.fees
                continue
            tot += mp.yes_q * y + mp.no_q * (1.0 - y) - mp.cost - mp.fees
        return tot

    def day_pnl(self, now: float) -> float:
        day = C.et_day(now)
        evs = set()
        for t in self.pos:
            ev = self._event_of(t)
            s = event_settle_ts(ev)
            if s is not None and C.et_day(s - 1.0) == day:
                evs.add(ev)
        return sum(self.event_pnl(e) for e in evs)

    def _check_losses(self, now: float) -> None:
        for ev in sorted({self._event_of(t) for t in self.pos}):
            if ev in self.halted_events:
                continue
            pnl = self.event_pnl(ev)
            if pnl <= -self.cfg.event_loss_halt:
                self.halt_event(now, ev, f"event loss ${pnl:.2f} <= -${self.cfg.event_loss_halt:g}")
        d = self.day_pnl(now)
        if d <= -self.cfg.daily_loss_halt:
            self.halt(now, f"daily loss ${d:.2f} <= -${self.cfg.daily_loss_halt:g} "
                           f"(ET day {C.et_day(now)})")

    def halt_event(self, now: float, ev: str, why: str) -> None:
        self.halted_events[ev] = why
        self.log(f"!! event {ev} halted: {why} -- its orders cancelled, no quotes (pads "
                 f"too) for the rest of the event", now)
        ops = [Op("cancel", o.ticker, o.side, o.price, o.remaining, o, True, "event_halt", o.tag)
               for o in list(self.orders.values()) if self._event_of(o.ticker) == ev]
        self._execute(ops, now)
        self._save_state(now)
        self._mail(f"Temp MM event halt: {ev}", f"{why}\n\nThe bot keeps quoting other events.")

    def _suspect(self, now: float, ticker: str, why: str, quiet: bool = False) -> None:
        """A fill on `ticker` may have been missed (an order of ours vanished,
        a cancel / amend found it gone, Kalshi's fill count ran ahead of our
        fills, the WebSocket dropped): new orders there wait for a fills
        sweep and a positions read taken after now (live only)."""
        if not self.live:
            return
        info = self.events.get(self._event_of(ticker))
        if info is not None and info.close_ts < now - 60.0:
            return
        if ticker not in self.suspect:
            # forced once; while it stays suspect the verify runs every verify_s
            # (a mismatch that persists costs 2 reads / 5 s, not a read storm)
            self.suspect[ticker] = now
            self.reader.force.update(("fills", "verify"))
            if not quiet:
                self.log(f"! {ticker}: {why} -- new orders there held until a fills sweep and "
                         f"a positions read agree", now)

    def _check_positions(self, ev: str, rows: List[dict], t_read: float, now: float) -> None:
        """Kalshi's positions on one event vs our fills. A market Kalshi does
        not list counts as flat. A mismatch makes the market suspect and
        forces a fills sweep and another read; the same mismatch after that
        sweep means fills are not reaching the bot: HALT (the risk caps and
        loss halts all run on our fills). A match clears a suspicion raised
        before the read."""
        k: Dict[str, float] = {}
        for r in rows:
            t = str(r.get("ticker") or r.get("market_ticker") or "")
            if self._event_of(t) != ev:
                continue
            k[t] = fpf(r.get("position_fp"), fpf(r.get("position")))
        tickers = set(k) | {t for t, mp in self.pos.items()
                            if self._event_of(t) == ev and (mp.yes_q or mp.no_q)}
        tickers |= {t for t in self.suspect if self._event_of(t) == ev}
        for t in sorted(tickers):
            k_net = k.get(t, 0.0)
            mp = self.pos.get(t, MarketPos())
            ours = mp.yes_q - mp.no_q
            if abs(k_net - ours) > 0.5:
                first = self.mismatch_at.get(t)
                if first is None:
                    self.mismatch_at[t] = t_read
                    self.pos_mismatch[t] = 1
                elif self.last_fills_read > first:
                    self.pos_mismatch[t] = 2
                self.log(f"! position mismatch {t}: Kalshi {k_net:+g}, our fills {ours:+g} "
                         f"(check {self.pos_mismatch[t]})", now)
                self._suspect(now, t, "position mismatch", quiet=True)
                if self.pos_mismatch[t] >= 2:
                    self.halt(now, f"position mismatch on {t}: Kalshi {k_net:+g} vs our fills "
                                   f"{ours:+g}, again after a fills sweep -- fills are not "
                                   f"reaching the bot, so its risk caps and loss halts are blind")
                    return
            else:
                self.pos_mismatch.pop(t, None)
                self.mismatch_at.pop(t, None)
                s = self.suspect.get(t)
                if s is not None and t_read >= s:
                    self.suspect.pop(t, None)
                    self.log(f"{t}: positions agree with our fills ({ours:+g}); orders resume", now)
                    self.need_decide = True

    # ---- orders: reconcile / confirm -----------------------------------
    def _reconcile_resting(self, rows: List[dict], t_read: float, now: float) -> None:
        self.last_resting_read = max(self.last_resting_read, t_read)
        seen: Set[str] = set()
        strays: List[Tuple[str, str]] = []
        for r in rows:
            oid = str(r.get("order_id") or "")
            coid = str(r.get("client_order_id") or "")
            if not coid.startswith(CLIENT_PREFIX + "-"):
                continue                     # never touch an order that is not ours
            seen.add(oid)
            o = self.orders.get(oid)
            t = str(r.get("ticker") or "")
            sa = r.get("subaccount_number")
            if sa is not None and self.sub and int(sa) != self.sub:
                self.halt(now, f"order {oid} rests in subaccount {sa}, not {self.sub}",
                          misbooked=[(oid, t, int(sa))])
                return
            if o is not None:
                if o.last_change < t_read - 2.0:
                    kf = fpf(r.get("fill_count_fp"), o.kfilled)
                    kr = fpf(r.get("remaining_count_fp"), o.remaining)
                    o.kfilled = max(o.kfilled, kf)
                    o.total = o.filled + kr
                    if kf > o.fills_seen + 0.5:
                        self._suspect(now, o.ticker, f"Kalshi reports {kf:g} filled on order "
                                                     f"{oid[:12]}, our fills {o.fills_seen:g}")
                    kexp = parse_ts(r.get("expiration_time") or r.get("expiration_ts"))
                    if kexp is not None and o.renewed and kexp < o.expires_at - 5.0:
                        if self.amend_expiry_ok:
                            self.log(f"! Kalshi kept order {oid[:12]}'s expiry at {C.iso(kexp)} "
                                     f"after an expiry-only amend: renewing by cancel + re-place "
                                     f"from now on", now)
                        self.amend_expiry_ok = False
                        o.expires_at = kexp
                continue
            amb = self.ambiguous.pop(coid, None)
            if amb is not None:
                self._adopt(r, amb, now)
                continue
            c_at = self.cancelled_ids.get(oid)
            if c_at is not None and c_at < t_read - 10.0:
                self.halt(now, f"order {oid} ({t}) still rests {t_read - c_at:.0f} s after Kalshi "
                               f"answered our cancel as done / not found -- cancels are not "
                               f"landing", misbooked=[(oid, t, self.sub)])
                return
            if self.closed.get(oid, -1e18) > t_read - 5.0:
                continue                     # a cancel of ours in flight at read time
            strays.append((oid, t))
        # ambiguous creates this read (taken after them) did not list never
        # booked -- or booked and filled at once: verify the market
        for coid, amb in list(self.ambiguous.items()):
            if t_read > amb["at"] + 1.0:
                self.ambiguous.pop(coid, None)
                self.cooled.pop((amb["ticker"], amb["side"], amb["price"]), None)
                self.log(f"ambiguous create {coid} ({amb['ticker']} {amb['side']} @{amb['price']}) "
                         f"is not resting", now)
                self._suspect(now, amb["ticker"], "an ambiguous create is not resting", quiet=True)
        for oid, o in list(self.orders.items()):
            if oid in seen or o.placed_at > t_read - 5.0 or o.last_change > t_read - 5.0:
                continue
            self._olog("gone", now, ticker=o.ticker, side=o.side, price=o.price,
                       size=o.remaining, oid=oid, reason="not_resting")
            self.log(f"order {oid[:12]} {o.ticker} {o.side} @{o.price} no longer rests "
                     f"(filled / expired / cancelled elsewhere)", now)
            self._forget(o, now)
            self._suspect(now, o.ticker, f"order {oid[:12]} vanished", quiet=True)
            self.need_decide = True
        if strays:
            self.log(f"! {len(strays)} untracked tmm- order(s) resting: cancelling", now)
            self._cancel_ids(strays, now, "stray")

    def _adopt(self, r: dict, amb: dict, now: float) -> None:
        """A create that got no answer turned out to rest: track it."""
        oid = str(r.get("order_id"))
        kf = fpf(r.get("fill_count_fp"))
        kr = fpf(r.get("remaining_count_fp"), amb["size"])
        o = Order(oid, amb["coid"], amb["ticker"], amb["side"], int(amb["price"]), kf + kr,
                  amb["at"], amb["exp"], amb["tag"], kfilled=kf, confirmed=True,
                  last_change=now)
        self.orders[oid] = o
        self.cooled.pop((amb["ticker"], amb["side"], amb["price"]), None)
        self._olog("adopt", now, oid=oid, coid=amb["coid"], ticker=o.ticker, side=o.side,
                   price=o.price, size=o.remaining)
        self.log(f"ambiguous create {amb['coid']} rests as {oid}: tracking it", now)
        if not self.first_confirmed:
            self.first_confirmed = True
            self.log(f"first order {oid} confirmed in subaccount {self.sub} (its subaccount's "
                     f"order list); quoting fully", now)
        if kf > 0.5:
            self._suspect(now, o.ticker, f"adopted order {oid[:12]} already filled {kf:g}")
        self.need_decide = True

    def _on_confirm(self, rows: List[dict], now: float) -> None:
        """rows: GetOrders with subaccount=N (the subaccount's own list)."""
        for r in rows:
            oid = str(r.get("order_id") or "")
            if oid not in self.unconfirmed:
                continue
            sa = r.get("subaccount_number")
            if sa is not None and int(sa) != self.sub:
                self.halt(now, f"order {oid} was booked to subaccount {sa}, not {self.sub}",
                          misbooked=[(oid, str(r.get("ticker") or ""), int(sa))])
                return
            self._confirmed(oid, now, "subaccount order list")

    def _on_readback(self, oid: str, rec: Optional[dict], now: float) -> None:
        """rec: GetOrder (not subaccount-scoped). Only subaccount_number == N
        confirms (nfl_snipe_bot's rule); a record without it is a miss."""
        if oid not in self.unconfirmed:
            return
        sa = None if rec is None else rec.get("subaccount_number")
        if sa is not None and int(sa) != self.sub:
            self.halt(now, f"order {oid} was booked to subaccount {sa}, not {self.sub}",
                      misbooked=[(oid, str(rec.get("ticker") or ""), int(sa))])
            return
        if sa is not None:
            self._confirmed(oid, now, "read-back")
        else:
            self.confirm_misses[oid] += 1

    def _confirmed(self, oid: str, now: float, how: str) -> None:
        self.unconfirmed.pop(oid, None)
        self.unconf_ticker.pop(oid, None)
        self.confirm_misses.pop(oid, None)
        o = self.orders.get(oid)
        if o is not None:
            o.confirmed = True
        if not self.first_confirmed:
            self.first_confirmed = True
            self.log(f"first order {oid} confirmed in subaccount {self.sub} ({how}); "
                     f"quoting fully", now)

    def _check_confirm_timeouts(self, now: float) -> None:
        """An order unconfirmed past confirm_timeout_s HALTs only when a read
        that succeeded after half the timeout still could not place it in our
        subaccount. While the reads fail (an outage) it does not halt: new
        orders are held instead (confirm_hold) until the reads come back."""
        if not self.live:
            return
        hold = False
        for oid, (ev, placed) in list(self.unconfirmed.items()):
            if now - placed <= self.cfg.confirm_timeout_s:
                continue
            if self.confirm_misses.get(oid, 0) >= 1:
                t = self.unconf_ticker.get(oid)
                self.halt(now, f"order {oid} ({ev}) could not be confirmed in subaccount "
                               f"{self.sub} within {self.cfg.confirm_timeout_s:g} s: subaccount "
                               f"{self.sub}'s order list does not show it and its read-back "
                               f"does not report subaccount {self.sub}; it may rest elsewhere",
                          probe=[(oid, t)] if t else None)
                return
            hold = True
        if hold and not self.confirm_hold:
            self.log("! orders unconfirmed past the timeout while the confirm reads fail: new "
                     "orders held until they come back", now)
        self.confirm_hold = hold

    def _readback_first(self, oid: str, ticker: str, now: float) -> None:
        """The session's first live order: read back at once, before any
        other order is placed (nfl_snipe_bot's rule: subaccount_number must
        be N). GetOrder takes no subaccount param, so a record without
        subaccount_number proves nothing: then the subaccount's own order
        list (GetOrders?subaccount=N&ticker=..) must show it."""
        try:
            rec = self.ex.get_order(oid, sync=True)
        except Exception as e:                           # noqa: BLE001
            rec = None
            self.log(f"! read-back of {oid} failed: {e}", now)
        sa = None if rec is None else rec.get("subaccount_number")
        if sa is not None:
            if int(sa) == self.sub:
                self._confirmed(oid, now, "first read-back")
            else:
                self.halt(now, f"first order {oid} was booked to subaccount {sa}, not {self.sub}",
                          misbooked=[(oid, ticker, int(sa))])
            return
        try:
            rows = self.ex.ticker_orders(ticker, sync=True)
        except Exception as e:                           # noqa: BLE001
            rows = []
            self.log(f"! subaccount order list read failed: {e}", now)
        for r in rows:
            if str(r.get("order_id")) != oid:
                continue
            rsa = r.get("subaccount_number")
            if rsa is not None and int(rsa) != self.sub:
                self.halt(now, f"first order {oid} was booked to subaccount {rsa}, not "
                               f"{self.sub}", misbooked=[(oid, ticker, int(rsa))])
                return
            self._confirmed(oid, now, "first read-back (subaccount order list)")
            return
        self.log(f"first order {oid} not confirmed yet; no further orders until it is "
                 f"(batched read every {self.cfg.confirm_s:g} s)", now)

    def _expire(self, now: float) -> None:
        for o in list(self.orders.values()):
            if now > o.expires_at + (0.0 if not self.live else 2.0):
                self._olog("expired", now, ticker=o.ticker, side=o.side, price=o.price,
                           size=o.remaining, oid=o.oid, dry=not self.live)
                self._forget(o, now)
                self.need_decide = True
        for oid, ts in list(self.closed.items()):
            if now - ts > 900.0:
                self.closed.pop(oid, None)
        for oid, ts in list(self.cancelled_ids.items()):
            if now - ts > 900.0:
                self.cancelled_ids.pop(oid, None)
        amb_keys = {(a["ticker"], a["side"], a["price"]) for a in self.ambiguous.values()}
        for k, ts in list(self.cooled.items()):
            if now - ts > 10.0 and k not in amb_keys:   # an ambiguous create: until a read
                self.cooled.pop(k, None)
        for k, ts in list(self.op_cool.items()):
            if now > ts:
                self.op_cool.pop(k, None)

    # ---- one event's decision ------------------------------------------
    def _event_ops(self, ev: EventInfo, now: float, flips: List[Tuple[str, bool, int]]) -> List[Op]:
        cfg = self.cfg
        guards: Dict[str, Any] = {}
        feed_ok = bool(self.feed.healthy())
        if not feed_ok:
            guards["ws_unhealthy"] = True
        fr = self.fair.result(ev, now, cfg.stale_wx_s)
        self.fair_last[ev.event] = fr
        if fr.stale:
            guards["wx_stale"] = fr.reason
        halted = ev.event in self.halted_events
        if halted:
            guards["event_halted"] = self.halted_events[ev.event]
        closing = now >= ev.close_ts - cfg.close_stop_s
        if closing:
            guards["closed"] = True
        if abs(ev.settle_gap_s) > 1.0:
            # the API's close_time and the ticker's hour disagree (a DST fall-back
            # hour?): the model would price the wrong hour -- quote nothing
            guards["settle_vs_close"] = f"{ev.settle_gap_s:+.0f}s"
            closing = True
            if ev.event not in self.settle_logged:
                self.settle_logged.add(ev.event)
                self.log(f"! {ev.event}: close_time {C.iso(ev.close_ts)} vs the ticker's hour "
                         f"{C.iso(ev.settle_ts)} ({ev.settle_gap_s:+.0f} s): not quoted", now)
        if not pol.quote_hour_ok(self.P, ev.settle_ts):
            # quote_hours_et: decide() rests nothing on this event (the
            # policy's own test, below); named here for the log and status
            guards["off_hours"] = pol.et_hour(ev.settle_ts)
            if ev.event not in self.hours_logged:
                self.hours_logged.add(ev.event)
                self.log(f"{ev.event}: settles {pol.et_hour(ev.settle_ts):02d}:00 ET, outside "
                         f"quote_hours_et {self.P.get('quote_hours_et')}: not quoted", now)
        P = self.P_stale if fr.stale else self.P
        states: List[pol.MarketState] = []
        meta: Dict[str, dict] = {}
        for strike, t in ev.markets:
            mg: List[str] = []
            lv = self.feed.book_levels(t) if feed_ok else None
            own_y, own_n = self.own_levels(t)
            trusted = lv is not None
            ext_y: Dict[int, float] = {}
            ext_n: Dict[int, float] = {}
            if trusted and self.live:
                # the live book holds our orders: the policy reads the book without us
                ext_y, ext_n = pol.external_book(cents_levels(lv[0]), cents_levels(lv[1]),
                                                 own_y, own_n)
            elif trusted:
                # dry: the dry book never reached the exchange (the shadow's view)
                ext_y, ext_n = cents_levels(lv[0]), cents_levels(lv[1])
            elif feed_ok:
                mg.append("book_untrusted")
            f = fr.fairs.get(t)
            if f is None and cfg.require_fair:
                trusted = False
                mg.append("no_fair")
            prog = self.programs.get(t)
            if prog is None and cfg.require_program and now >= ev.open_ts:
                mg.append("no_program")
            mp = self.pos.get(t) or MarketPos()
            # Kalshi's fill count ahead of the fills we booked (a resting
            # read saw it): counted at once at the order's price, so the
            # risk caps never lag the exchange (live; 0 in dry)
            py = pn = pc = 0.0
            for o in self._orders_of(t):
                extra = o.kfilled - o.fills_seen
                if extra > 0.5:
                    if o.side == "yes":
                        py += extra
                    else:
                        pn += extra
                    pc += extra * o.price / 100.0
            st = pol.MarketState(
                ticker=t, strike=strike, book_yes=ext_y, book_no=ext_n, trusted=trusted,
                fair=f, slope_c=fr.slopes.get(t),
                program_active=(False if (prog is None and cfg.require_program) else None),
                program_start=prog["start"] if prog else None,
                program_end=prog["end"] if prog else None,
                target=prog["target"] if prog else pol.TARGET,
                df=prog["df"] if prog else pol.DF,
                yes_pos=mp.yes_q + py, no_pos=mp.no_q + pn, cost=mp.cost + pc,
                resting_yes=own_y, resting_no=own_n)
            states.append(st)
            meta[t] = {"ext_y": ext_y, "ext_n": ext_n, "guards": mg}
            for g in mg:
                self.guard_counts[g] += 1
            # marks for the loss checks: the external mid, else the fair
            bv = pol.BookView(ext_y, ext_n) if trusted else None
            if bv is not None and bv.two_sided:
                self.marks[t] = bv.mid / 100.0
            elif f is not None:
                self.marks[t] = f
        lat = float(P["order_latency_s"])
        ctx = pol.EventContext(now=now, close_ts=ev.close_ts, rest_from=now + lat,
                               rest_until=now + lat + cfg.pull_lead_s, open_ts=ev.open_ts,
                               settle_ts=ev.settle_ts)
        dec = pol.decide(P, ctx, states)
        self.resting_wcl[ev.event] = dec.resting_wcl
        for g in guards:
            self.guard_counts[g] += 1
        self.guards_now[ev.event] = dict(guards)
        # the landing-window pull: logged when the policy's flag flips
        prev = self.wflag_prev.get(ev.event)
        if prev is not None and prev != dec.wflag and P["window_mode"] != "none":
            n_live = sum(1 for o in self.orders.values()
                         if self._event_of(o.ticker) == ev.event
                         and set(o.tag.split("+")) & {"live", "near"})
            flips.append((ev.event, dec.wflag, n_live))
        self.wflag_prev[ev.event] = dec.wflag
        ops: List[Op] = []
        sample = now - self.last_sample.get(ev.event, -1e18) >= cfg.decisions_s
        for md in dec.markets:
            want_y, want_n = dict(md.yes), dict(md.no)
            if halted or closing or not feed_ok:
                want_y, want_n = {}, {}
            for d in (want_y, want_n):
                for p in list(d):
                    if d[p] > cfg.max_contracts_order:
                        d[p] = float(cfg.max_contracts_order)
            tags = {(o.side, o.price): o.tag for o in md.orders}
            m = meta[md.ticker]
            stable = {}
            for side, want in (("yes", want_y), ("no", want_n)):
                # the 1c pads never reprice (a pad only pairs with a pad price),
                # so a pad appearing -- the pad hold ending, a thin side's
                # qualification pad -- does not restart the debounce of the
                # favorite / live prices beside it
                key = tuple(sorted(p for p in want
                                   if tag_kind(tags.get((side, p), "")) != "pad"))
                prev = self.want_since.get((md.ticker, side))
                if prev is None or prev[0] != key:
                    self.want_since[(md.ticker, side)] = prev = (key, now)
                stable[side] = now - prev[1] >= cfg.reprice_debounce_s
            ops += plan_market(md.ticker, self._orders_of(md.ticker), want_y, want_n, tags, now,
                               cfg, m["ext_y"], m["ext_n"], guard_caps(P, md.diag), stable)
            if sample:
                self._sample(ev, md, dec, fr, m, guards, want_y, want_n, now)
        if sample:
            self.last_sample[ev.event] = now
        return ops

    def _sample(self, ev: EventInfo, md, dec, fr: FairResult, m: dict, guards: dict,
                want_y: dict, want_n: dict, now: float) -> None:
        own_y, own_n = self.own_levels(md.ticker)
        bv = pol.BookView(m["ext_y"], m["ext_n"])
        mp = self.pos.get(md.ticker) or MarketPos()
        d = md.diag or {}
        self.log.row("decisions", {
            "kind": "sample", "event": ev.event, "ticker": md.ticker, "strike": md.strike,
            "tau_min": round(dec.tau_min, 3), "second": round(now % 60.0, 2),
            "fair": None if fr.fairs.get(md.ticker) is None else round(fr.fairs[md.ticker], 5),
            "slope_c": None if fr.slopes.get(md.ticker) is None else round(fr.slopes[md.ticker], 3),
            "latest_minute": C.iso(fr.latest_minute_ms / 1000.0) if fr.latest_minute_ms else None,
            "index": fr.index, "mu": fr.mu, "sigma": fr.sigma,
            "cls": md.cls, "rule": md.rule, "reason": d.get("reason"), "wflag": dec.wflag,
            "pflag": d.get("pflag"), "size_full": dec.size_full,
            "best_y": bv.best_y, "best_n": bv.best_n, "ref_y": bv.ref_y, "ref_n": bv.ref_n,
            "depth_y": round(bv.sum_y, 2), "depth_n": round(bv.sum_n, 2),
            "want_yes": {str(k): v for k, v in sorted(want_y.items(), reverse=True)},
            "want_no": {str(k): v for k, v in sorted(want_n.items(), reverse=True)},
            "tags": [f"{o.side}@{o.price}:{o.tag}" for o in md.orders],
            "resting_yes": {str(k): v for k, v in own_y.items()},
            "resting_no": {str(k): v for k, v in own_n.items()},
            "guards": sorted(set(guards) | set(m["guards"])),
            "pos": [mp.yes_q, mp.no_q, round(mp.cost, 2)],
            "wcl": round(dec.worst_case_loss, 2), "resting_wcl": round(dec.resting_wcl, 2),
            "pad_hold": dec.pad_hold, "quote_hour": dec.quote_hour,
            "fav_market": d.get("fav_market"),
            "suspect": md.ticker in self.suspect}, now)

    # ---- execution -------------------------------------------------------
    def _execute(self, ops: List[Op], now: float) -> None:
        if not ops:
            return
        if now < self.write_backoff_until:
            self.deferred["backoff"] += len(ops)
            return
        urgent = [o for o in ops if o.urgent]
        paced = [o for o in ops if not o.urgent]
        # several guard-forced reprices at once (a station row moved the fair
        # under k resting orders): one batched cancel takes them all off in
        # one round trip, and post_only creates re-place them (paced), rather
        # than k blocking amends in a row
        guard = [o for o in urgent if o.kind == "amend" and o.reason == "reprice"
                 and not o.renew and o.order is not None]
        if len(guard) >= 2:
            gids = {id(o) for o in guard}
            urgent = [o for o in urgent if id(o) not in gids]
            for op in guard:
                o = op.order
                urgent.append(Op("cancel", op.ticker, op.side, o.price, o.remaining, o, True,
                                 "guard", o.tag))
                paced.append(Op("create", op.ticker, op.side, op.price, op.size, None, False,
                                "guard", op.tag))
        cancels = [o for o in urgent if o.kind == "cancel"]
        if cancels:
            self._do_cancels(cancels, now)
        # the most exposed first (price over the guard cap x size), renewals last
        for op in sorted((o for o in urgent if o.kind != "cancel"),
                         key=lambda o: (o.renew, -o.excess)):
            if self.halt_reason or now < self.write_backoff_until:
                return
            self._do(op, now)
        if self.halt_reason or now < self.write_backoff_until:
            return
        acted: Set[str] = {o.ticker for o in urgent if o.kind != "cancel" and not o.renew}
        if not paced:
            for t in acted:
                self.last_act[t] = now
            return
        for dq in (self.new_orders, self.writes):
            while dq and dq[0] < now - 60.0:
                dq.popleft()
        allowed = {t for t in {o.ticker for o in paced}
                   if now - self.last_act.get(t, -1e18) >= self.cfg.min_act_s
                   and t not in acted}
        for op in paced:
            if self.halt_reason or now < self.write_backoff_until:
                return
            if op.ticker not in allowed:
                self.deferred["min_act"] += 1
                continue
            grows = op.kind == "create" or (op.kind == "amend" and op.order is not None
                                            and op.size > op.order.remaining + 0.5)
            if grows and op.ticker in self.suspect:
                self.deferred["suspect"] += 1
                continue
            if op.kind == "create" and self.confirm_hold:
                self.deferred["confirm_hold"] += 1
                continue
            if op.kind == "create" and not self.first_confirmed and (self.unconfirmed
                                                                     or self.ambiguous):
                self.deferred["first_unconfirmed"] += 1
                continue
            if op.kind == "create" and len(self.new_orders) >= self.cfg.max_new_orders_min:
                self.deferred["new_orders_cap"] += 1
                continue
            if len(self.writes) >= self.cfg.max_writes_min:
                self.deferred["writes_cap"] += 1
                continue
            if not self.wbucket.try_take(now):
                self.deferred["write_rate"] += 1
                continue
            if self._do(op, now) and not op.renew:
                acted.add(op.ticker)
        for t in acted:
            self.last_act[t] = now

    def _fundable(self, price: int, size: int) -> int:
        if self.cash is None:
            return size if not self.live else 0
        avail = self.cash - self.cash_spent - self.cfg.cash_reserve
        if price <= 0:
            return size
        return max(0, min(size, int(math.floor(avail * 100.0 / price + 1e-9))))

    def _count_write(self, now: float, create: bool = False) -> None:
        self.writes.append(now)
        if create:
            self.new_orders.append(now)

    def _do(self, op: Op, now: float) -> bool:
        key = (op.order.oid if op.order is not None else f"{op.ticker}|{op.side}|{op.price}",
               op.kind)
        if op.kind != "cancel" and now < self.op_cool.get(key, -1e18):
            self.deferred["op_cool"] += 1           # rejected moments ago: not resent yet
            return False
        try:
            if op.kind == "create":
                return self._create(op, now)
            if op.kind == "amend":
                return self._amend(op, now)
            if op.kind == "decrease":
                return self._decrease(op, now)
            if op.kind == "cancel":
                self._do_cancels([op], now)
                return True
        except Exception as e:                           # noqa: BLE001
            self.log(f"! {op.kind} {op.ticker} failed: {type(e).__name__}: {e}", now)
            self._order_error(now, op.kind, _api_error(e))
        return False

    def _create(self, op: Op, now: float) -> bool:
        size = min(int(round(op.size)), self.cfg.max_contracts_order)
        if (op.ticker, op.side, op.price) in self.cooled:
            self.deferred["cooled"] += 1
            return False
        fund = self._fundable(op.price, size)
        if fund < size:
            self.guard_counts["cash_trim"] += 1
            if fund < 1:
                self.deferred["cash"] += 1
                return False
            size = fund
        if size < 1:
            return False
        coid = f"{CLIENT_PREFIX}-{uuid.uuid4().hex[:24]}"
        exp = int(now + ttl_for(self.cfg, op.tag))
        row = dict(ticker=op.ticker, side=op.side, price=op.price, size=size, tag=op.tag,
                   reason=op.reason, coid=coid, expiration_ts=exp)
        self._count_write(now, create=True)
        self.op_counts["create"] += 1
        self.day_counts["create"] += 1
        if not self.live:
            oid = "dry-" + uuid.uuid4().hex[:16]
            self.orders[oid] = Order(oid, coid, op.ticker, op.side, op.price, float(size), now, exp,
                                     op.tag, confirmed=True, last_change=now)
            self._olog("place", now, oid=oid, dry=True, **row)
            self.log(f"[DRY] would place {op.ticker} {op.side.upper()} {size} @ {op.price}c "
                     f"[{op.tag}] ({op.reason})", now)
            return True
        t0 = time.perf_counter()
        try:
            resp = self.ex.create(op.ticker, op.side, op.price, size, coid, exp)
        except ApiError as e:
            body = e.body or ""
            cross = "post" in body.lower() and "cross" in body.lower()
            cls = err_class(e)
            # no answer, or a gateway error that may have reached the matching
            # engine: the order MAY rest (a 503 is Kalshi refusing service)
            unsure = cls == "unavailable" and e.status != 503
            self._olog("uncertain" if unsure else "reject", now, error=str(e),
                       error_body=body[:300], http_status=e.status, **row)
            self.log(f"! place {'uncertain' if unsure else 'rejected'} "
                     f"{op.ticker} {op.side} {size} @ {op.price}c: {e} {body[:160]}", now)
            if unsure:
                self._ambiguous(now, coid, op, size, exp)
                self._backoff(now, cls, f"create: {e}")
            elif cls in TRANSIENT:
                self._backoff(now, cls, f"create: {e} {body[:120]}")
            else:
                if not cross:
                    self.op_cool[(f"{op.ticker}|{op.side}|{op.price}", "create")] = \
                        now + self.cfg.op_cool_s
                self._order_error(now, "create", e, counted=not cross)
            return False
        self._write_ok(now)
        oid = str(resp.get("order_id") or (resp.get("order") or {}).get("order_id") or "")
        if not oid:
            self._olog("uncertain", now, error="no order_id in response", response=resp, **row)
            self._ambiguous(now, coid, op, size, exp)
            return False
        self.traded[self._event_of(op.ticker)] = now
        filled = fpf(resp.get("fill_count"))
        o = Order(oid, coid, op.ticker, op.side, op.price, float(size), now, exp, op.tag,
                  kfilled=filled, last_change=now)
        self.orders[oid] = o
        self.unconfirmed[oid] = (self._event_of(op.ticker), now)
        self.unconf_ticker[oid] = op.ticker
        self._olog("place", now, oid=oid, latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                   fill_count=filled, **row)
        self.log(f"placed {op.ticker} {op.side.upper()} {size} @ {op.price}c [{op.tag}] "
                 f"-> {oid}", now)
        if filled > 0:
            self.log(f"! post-only order {oid} reported fill_count {filled}", now)
            self.reader.force.add("fills")
        if not self.first_confirmed and len(self.unconfirmed) == 1:
            self._readback_first(oid, op.ticker, now)
        return True

    def _ambiguous(self, now: float, coid: str, op: Op, size: int, exp: int) -> None:
        """A create with no answer (timeout, 5xx, no order id): it MAY rest.
        Its client id is kept and a resting read forced at once: if the order
        rests it is adopted (tracked), if not it never booked. Until then
        the price stays cooled (no second create)."""
        self.ambiguous[coid] = dict(coid=coid, ticker=op.ticker, side=op.side, price=op.price,
                                    size=size, tag=op.tag, exp=exp, at=now)
        self.cooled[(op.ticker, op.side, op.price)] = now
        self.traded[self._event_of(op.ticker)] = now
        self.reader.force.add("resting")

    def _amend(self, op: Op, now: float) -> bool:
        o = op.order
        if o is None or o.oid not in self.orders:
            return False
        if op.renew and not self.amend_expiry_ok:
            # replace instead: cancel now, the next decision places it again
            self._do_cancels([Op("cancel", o.ticker, o.side, o.price, o.remaining, o, True,
                                 "renew_replace", o.tag)], now)
            return True
        new_rem = min(int(round(op.size)), self.cfg.max_contracts_order)
        if new_rem < 1:
            self._do_cancels([Op("cancel", o.ticker, o.side, o.price, o.remaining, o, True,
                                 op.reason, o.tag)], now)
            return True
        if op.price * new_rem > o.price * o.remaining + EPS:
            fund = self._fundable(op.price, new_rem)
            if fund < new_rem:
                self.guard_counts["cash_trim"] += 1
                if fund < 1:
                    self.deferred["cash"] += 1
                    return False
                new_rem = fund
        tag = op.tag or o.tag
        # only a renewal carries an expiry: a reprice keeps the order's own
        exp = int(now + ttl_for(self.cfg, tag)) if op.renew else None
        total = o.filled + new_rem
        row = dict(ticker=o.ticker, side=o.side, oid=o.oid, prev_price=o.price, price=op.price,
                   prev_size=o.remaining, size=new_rem, total=total, tag=tag, reason=op.reason,
                   expiration_ts=exp, renew=op.renew, urgent=op.urgent)
        self._count_write(now)
        self.op_counts["renew" if op.renew else "amend"] += 1
        self.day_counts["amend"] += 1
        if not self.live:
            o.price, o.total, o.tag, o.last_change = op.price, total, tag, now
            if exp is not None:
                o.expires_at = exp
            self._olog("renew" if op.renew else "amend", now, dry=True, **row)
            if not op.renew:
                self.log(f"[DRY] would amend {o.ticker} {o.side.upper()} {row['prev_size']:g} @ "
                         f"{row['prev_price']}c -> {new_rem} @ {op.price}c [{tag}] ({op.reason})",
                         now)
            return True
        t0 = time.perf_counter()
        try:
            resp = self.ex.amend(o, op.price, total, exp)
        except ApiError as e:
            self._olog("amend_error", now, error=str(e), error_body=e.body[:300],
                       http_status=e.status, **row)
            cls = err_class(e)
            if cls == "gone":
                self._olog("gone", now, ticker=o.ticker, side=o.side, price=o.price,
                           size=o.remaining, oid=o.oid, reason="amend_404")
                self._forget(o, now)
                self._suspect(now, o.ticker, f"amend found order {o.oid[:12]} gone", quiet=True)
                return True
            if op.renew and e.status == 400:
                self.amend_expiry_ok = False
                self.log(f"! expiry-only amend refused ({e} {e.body[:120]}); renewing by "
                         f"cancel + re-place from now on", now)
                self._do_cancels([Op("cancel", o.ticker, o.side, o.price, o.remaining, o, True,
                                     "renew_replace", o.tag)], now)
                return True
            if cls in TRANSIENT:
                self._backoff(now, cls, f"amend: {e} {e.body[:120]}")
                if op.urgent and not op.renew:
                    o.must_cancel = True        # off at the next write chance
                return False
            self.log(f"! amend failed {o.oid[:12]} {o.ticker}: {e} {e.body[:160]}", now)
            self._order_error(now, "amend", e)
            if op.urgent and not op.renew and not self.halt_reason:
                # the order rests at a price the guard no longer allows: off now
                self.log(f"! urgent reprice of {o.oid[:12]} refused: cancelling it", now)
                self._do_cancels([Op("cancel", o.ticker, o.side, o.price, o.remaining, o, True,
                                     "amend_failed", o.tag)], now)
                return True
            self.op_cool[(o.oid, "amend")] = now + self.cfg.op_cool_s
            return False
        self._write_ok(now)
        o.price, o.total, o.tag, o.last_change = op.price, total, tag, now
        if exp is not None:
            o.expires_at = exp
            o.renewed = True
            rec = resp.get("order") if isinstance(resp.get("order"), dict) else resp
            kexp = parse_ts(rec.get("expiration_time") or rec.get("expiration_ts"))
            if kexp is not None and kexp < exp - 5.0:
                self.amend_expiry_ok = False
                o.expires_at = kexp
                self.log(f"! the expiry-only amend of {o.oid[:12]} kept {C.iso(kexp)}: renewing "
                         f"by cancel + re-place from now on", now)
        if resp.get("remaining_count") not in (None, ""):
            o.total = o.filled + fpf(resp.get("remaining_count"))
        if fpf(resp.get("fill_count")) > 0:
            self.log(f"! amend of {o.oid} filled {resp.get('fill_count')} (crossed)", now)
            self.reader.force.add("fills")
        self._olog("renew" if op.renew else "amend", now,
                   latency_ms=round((time.perf_counter() - t0) * 1000, 1), **row)
        return True

    def _decrease(self, op: Op, now: float) -> bool:
        o = op.order
        if o is None or o.oid not in self.orders:
            return False
        k = int(round(op.size))
        if k >= o.remaining - 0.5:
            self._do_cancels([Op("cancel", o.ticker, o.side, o.price, o.remaining, o, True,
                                 op.reason, o.tag)], now)
            return True
        if k < 1:
            return False
        row = dict(ticker=o.ticker, side=o.side, oid=o.oid, price=o.price, prev_size=o.remaining,
                   size=o.remaining - k, reduce_by=k, reason=op.reason, tag=o.tag)
        self._count_write(now)
        self.op_counts["decrease"] += 1
        self.day_counts["decrease"] += 1
        if not self.live:
            o.total -= k
            o.last_change = now
            self._olog("decrease", now, dry=True, **row)
            self.log(f"[DRY] would decrease {o.ticker} {o.side.upper()} @ {o.price}c by {k} "
                     f"-> {o.remaining:g} ({op.reason})", now)
            return True
        try:
            resp = self.ex.decrease(o, k)
        except ApiError as e:
            self._olog("decrease_error", now, error=str(e), error_body=e.body[:300],
                       http_status=e.status, **row)
            cls = err_class(e)
            if cls == "gone":
                self._forget(o, now)
                self._suspect(now, o.ticker, f"decrease found order {o.oid[:12]} gone", quiet=True)
                return True
            if cls in TRANSIENT:
                self._backoff(now, cls, f"decrease: {e} {e.body[:120]}")
                return False
            self._order_error(now, "decrease", e)
            if not self.halt_reason:
                # a size cut is risk: when Kalshi refuses it, the order comes off
                self._do_cancels([Op("cancel", o.ticker, o.side, o.price, o.remaining, o, True,
                                     "decrease_failed", o.tag)], now)
            return True
        self._write_ok(now)
        o.total -= k
        if resp.get("remaining_count") not in (None, ""):
            o.total = o.filled + fpf(resp.get("remaining_count"))
        o.last_change = now
        self._olog("decrease", now, **row)
        return True

    def _do_cancels(self, ops: List[Op], now: float) -> None:
        live_ops = [op for op in ops if op.order is not None and op.order.oid in self.orders]
        if not live_ops:
            return
        for op in live_ops:
            self.op_counts["cancel"] += 1
            self.day_counts["cancel"] += 1
        if not self.live:
            for op in live_ops:
                o = op.order
                self._olog("cancel", now, ticker=o.ticker, side=o.side, price=o.price,
                           size=o.remaining, oid=o.oid, tag=o.tag, reason=op.reason, dry=True)
                self.log(f"[DRY] would cancel {o.ticker} {o.side.upper()} {o.remaining:g} @ "
                         f"{o.price}c [{o.tag}] ({op.reason})", now)
                self._forget(o, now)
            return
        items = [(op.order.oid, op.order.ticker) for op in live_ops]
        done = self._cancel_ids(items, now, live_ops[0].reason if len(set(op.reason for op in live_ops)) == 1 else "mixed")
        for op in live_ops:
            if op.order.oid in done:
                self._forget(op.order, now)

    def _cancel_ids(self, items: List[Tuple[str, str]], now: float, reason: str) -> Set[str]:
        """Cancel order ids (live): one batched request for two or more.
        Only a 404 on a single cancel means "already gone": a batch item's
        not_found is confirmed by a single cancel first (a batch that
        ignored the per-item subaccount would answer not_found for orders
        that still rest), and a gone order makes its market suspect (it may
        have filled unseen). A 409 is NOT gone: Kalshi did not process it
        (paused). 429 / 409 / 5xx / network start the write backoff: the
        orders stay tracked and are cancelled after it (no one-by-one storm
        into a throttle). Returns the ids that are gone; they are remembered
        (cancelled_ids): one still resting > 10 s later halts the bot."""
        if not items:
            return set()
        if not self.live:
            for oid, t in items:
                self._olog("cancel", now, oid=oid, ticker=t, reason=reason, dry=True)
            return {oid for oid, _ in items}
        gone: Set[str] = set()
        if now < self.write_backoff_until:
            self.deferred["backoff"] += len(items)
            return gone
        rest = list(items)
        if len(items) >= 2:
            try:
                res = self.ex.batch_cancel(items)
                self._write_ok(now)
                by = {str(r.get("order_id")): r for r in res}
                rest = []
                for oid, t in items:
                    r = by.get(oid)
                    if r is not None and not r.get("error"):
                        gone.add(oid)
                        self._olog("cancel", now, oid=oid, ticker=t, reason=reason, batched=True,
                                   reduced_by=r.get("reduced_by"))
                        continue
                    cls = item_error_class(r.get("error")) if r is not None else "reject"
                    if cls in TRANSIENT:
                        self._olog("cancel_error", now, oid=oid, ticker=t, reason=reason,
                                   batched=True, error=r.get("error") if r else None)
                        self._backoff(now, cls, f"batch cancel item: {r.get('error') if r else ''}")
                        continue
                    rest.append((oid, t))       # not_found / unknown: a single cancel decides
            except ApiError as e:
                cls = err_class(e)
                if cls in TRANSIENT:
                    self._olog("cancel_error", now, reason=reason, batched=True, error=str(e),
                               error_body=e.body[:300], http_status=e.status, n=len(items))
                    self._backoff(now, cls, f"batch cancel: {e} {e.body[:120]}")
                    return gone
                self.log(f"! batch cancel failed ({e} {e.body[:120]}); cancelling one by one", now)
                rest = list(items)
        for oid, t in rest:
            if now < self.write_backoff_until:
                break
            try:
                r = self.ex.cancel(oid, t)
                self._write_ok(now)
                gone.add(oid)
                self._olog("cancel", now, oid=oid, ticker=t, reason=reason,
                           reduced_by=(r or {}).get("reduced_by"))
            except ApiError as e:
                cls = err_class(e)
                if cls == "gone":
                    gone.add(oid)
                    self._olog("gone", now, oid=oid, ticker=t, reason=reason,
                               http_status=e.status)
                    self._suspect(now, t, f"cancel found order {oid[:12]} gone", quiet=True)
                    continue
                self._olog("cancel_error", now, oid=oid, ticker=t, reason=reason,
                           error=str(e), error_body=e.body[:300], http_status=e.status)
                if cls in TRANSIENT:
                    self._backoff(now, cls, f"cancel: {e} {e.body[:120]}")
                else:
                    self.log(f"! cancel failed {oid}: {e} {e.body[:160]}", now)
                    self._order_error(now, "cancel", e)
        for oid in gone:
            self.cancelled_ids[oid] = now
        if gone:
            self.log(f"cancelled {len(gone)} order(s) ({reason})", now)
        return gone

    def _cancel_strays_sync(self, now: float, why: str) -> int:
        """Every resting tmm- order in the subaccount (live): a REST read,
        then cancels. Used at startup, shutdown and halts."""
        if not self.live:
            return 0
        try:
            rows = self.ex.resting()
        except Exception as e:                           # noqa: BLE001
            self.log(f"! resting read failed ({why}): {e}", now)
            return 0
        items = [(str(r.get("order_id")), str(r.get("ticker") or "")) for r in rows
                 if str(r.get("client_order_id") or "").startswith(CLIENT_PREFIX + "-")]
        items = [(oid, t) for oid, t in items if oid not in self.orders]
        if items:
            self.log(f"{len(items)} tmm- order(s) resting in subaccount {self.sub} ({why}): "
                     f"cancelling", now)
            saved, self.write_backoff_until = self.write_backoff_until, 0.0
            try:
                self._cancel_ids(items, now, why)
            finally:
                self.write_backoff_until = max(saved, self.write_backoff_until)
        return len(items)

    def cancel_all(self, now: float, reason: str, sweep: bool = False) -> Set[str]:
        """Cancel every tracked order (and, with sweep, every resting tmm-
        order in the subaccount). Ignores the write backoff (a halt or a
        shutdown tries once). Returns the tracked ids still not cancelled."""
        ops = [Op("cancel", o.ticker, o.side, o.price, o.remaining, o, True, reason, o.tag)
               for o in list(self.orders.values())]
        if ops:
            saved, self.write_backoff_until = self.write_backoff_until, 0.0
            try:
                self._do_cancels(ops, now)
            finally:
                self.write_backoff_until = max(saved, self.write_backoff_until)
        left = {op.order.oid for op in ops if op.order.oid in self.orders}
        if sweep:
            self._cancel_strays_sync(now, reason)
        return left

    # ---- housekeeping ----------------------------------------------------
    def _roll_day(self, now: float) -> None:
        d = C.et_day(now)
        if d == self.et_day:
            return
        prev = self.et_day
        row = {"day": prev, "mode": "live" if self.live else "dry", "variant": self.variant,
               "subaccount": self.sub, **dict(self.day_counts),
               "pnl_day": round(self._pnl_for_day(prev), 2),
               "halted_events": sorted(e for e in self.halted_events
                                       if C.et_day((event_settle_ts(e) or 0) - 1.0) == prev)}
        self.log.row("summary", row, now)
        try:
            with open(os.path.join(self.dir, "summary.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except OSError:
            pass
        self.log(f"ET day {prev} done: {json.dumps(row, default=str)}", now)
        self.day_counts = Counter()
        self.et_day = d
        # per-market state of events settled > 36 h ago is no longer needed
        old = {t for t in set(self.pos) | set(self.marks) | set(self.results)
               if (event_settle_ts(self._event_of(t)) or now) < now - 36 * 3600.0}
        for t in old:
            self.pos.pop(t, None)
            self.marks.pop(t, None)
            self.results.pop(t, None)
        for k in [k for k in self.want_since if k[0] not in self._feed_tickers(now)]:
            self.want_since.pop(k, None)
        for t in [t for t in self.last_act if t not in self._feed_tickers(now)]:
            self.last_act.pop(t, None)

    def _pnl_for_day(self, day: str) -> float:
        evs = {self._event_of(t) for t in self.pos
               if C.et_day((event_settle_ts(self._event_of(t)) or 0) - 1.0) == day}
        return sum(self.event_pnl(e) for e in evs)

    def _alerts(self, now: float) -> None:
        cfg = self.cfg
        if (self.unhealthy_since is not None and not self.unhealthy_mailed
                and now - self.unhealthy_since >= cfg.unhealthy_alert_s):
            self.unhealthy_mailed = True
            self._mail("Temp MM: Kalshi writes failing",
                       f"Writes have failed for {(now - self.unhealthy_since) / 60:.0f} min "
                       f"({dict(self.backoff_counts)}: 429 throttle / 409 paused / 5xx / network). "
                       f"The bot retries every {cfg.backoff_max_s:g} s and resumes by itself; "
                       f"what rests expires by its TTL. Nothing to do unless it persists.")
        if self.suspect:
            oldest = min(self.suspect.values())
            if now - oldest >= cfg.unhealthy_alert_s:
                self._note_mail(f"suspect:{int(oldest)}", "Temp MM: markets held (fills unverified)",
                                f"{len(self.suspect)} market(s) have been held for "
                                f"{(now - oldest) / 60:.0f} min: a fill may have been missed and "
                                f"the fills sweep / positions reads have not confirmed our "
                                f"position ({sorted(self.suspect)[:6]}). Check the log in "
                                f"{self.dir}.")
        unk = getattr(self.fair, "unknown_cal_versions", None)
        if unk:
            msg = (f"station rows name calibration version(s) {unk} that the loaded calibrations "
                   f"({getattr(self.fair, 'cals_source', '?')}) lack: 4-station replicas may be "
                   f"off by ~0.0x F until the shadow's calibrations.json is refreshed")
            if f"cal:{unk}" not in self.mailed_notes:
                self.log(f"! {msg}", now)
            self._note_mail(f"cal:{unk}", "Temp MM: unknown station calibration", msg)

    def _housekeeping(self, now: float, state: Optional[str] = None) -> None:
        if now - self.last_status >= self.cfg.status_s:
            self.last_status = now
            self._status(now, state=state)
        if now - self.last_save >= 30.0:
            self.last_save = now
            self._save_state(now)
        if now - self.last_minute_log >= 60.0:
            self.last_minute_log = now
            self._alerts(now)
            for k in [k for k in self.want_since if k[0] not in self._feed_tickers(now)]:
                self.want_since.pop(k, None)
            lat = [x for t, x in self.lat if t >= now - 60.0]
            lat.sort()
            n = len(self.orders)
            self.log(f"minute: {n} open orders, decisions {self.decides}, loop p50 "
                     f"{(lat[len(lat) // 2] * 1000 if lat else 0):.1f} ms max "
                     f"{(lat[-1] * 1000 if lat else 0):.1f} ms, wx {self.wx.status(now).get('source')}"
                     f", REST last min {self.stats.last_minute(now)}, guards now "
                     f"{ {e: list(g) for e, g in self.guards_now.items() if g} }, day P&L "
                     f"${self.day_pnl(now):+.2f}", now)
        while self.lat and self.lat[0][0] < now - 60.0:
            self.lat.popleft()

    def _save_state(self, now: float) -> None:
        try:
            write_json_atomic(self.state_path, {
                "at": C.iso(now), "mode": "live" if self.live else "dry", "subaccount": self.sub,
                "halted_events": self.halted_events, "results": self.results})
        except OSError:
            pass

    def _load_state(self, now: float) -> None:
        js = C.read_json(self.state_path, None)
        if not isinstance(js, dict):
            return
        for e, why in (js.get("halted_events") or {}).items():
            s = event_settle_ts(e)
            if s is not None and s > now - 86400.0:
                self.halted_events[e] = why
        self.results.update(js.get("results") or {})

    def status(self, now: float, state: Optional[str] = None) -> dict:
        lat = sorted(x for t, x in self.lat if t >= now - 60.0)
        react = sorted(self.react)
        q = lambda a, f: round(a[min(len(a) - 1, int(f * len(a)))] * 1000, 2) if a else None
        ev_out = {}
        hold_s = float(self.P.get("pad_hold_open_s") or 0.0)
        for ev in self._quotable_events(now):
            os_ = [o for o in self.orders.values() if self._event_of(o.ticker) == ev.event]
            fr = self.fair_last.get(ev.event)
            ev_out[ev.event] = {
                "open": C.iso(ev.open_ts), "close": C.iso(ev.close_ts),
                "programs": sum(1 for _, t in ev.markets if t in self.programs),
                "orders": len(os_),
                "resting_yes": round(sum(o.remaining for o in os_ if o.side == "yes"), 2),
                "resting_no": round(sum(o.remaining for o in os_ if o.side == "no"), 2),
                "by_tag": dict(Counter(o.tag for o in os_)),
                "pnl": round(self.event_pnl(ev.event), 2),
                "halted": self.halted_events.get(ev.event),
                "guards": self.guards_now.get(ev.event, {}),
                "resting_wcl": round(self.resting_wcl.get(ev.event, 0.0), 2),
                "pad_hold_until": C.iso(ev.open_ts + hold_s) if hold_s > 0 else None,
                "quote_hour": pol.quote_hour_ok(self.P, ev.settle_ts),
                "fair": ({t: round(f, 4) for t, f in fr.fairs.items()} if fr else {}),
                "wx_latest_minute": (C.iso(fr.latest_minute_ms / 1000.0)
                                     if fr and fr.latest_minute_ms else None)}
        pos = {t: {"yes": mp.yes_q, "no": mp.no_q, "cost": round(mp.cost, 2), "fills": mp.fills}
               for t, mp in self.pos.items() if (mp.yes_q or mp.no_q)
               and self._event_of(t) in {e.event for e in self._quotable_events(now)}}
        try:
            feed_st = self.feed.status()
        except Exception:                                # noqa: BLE001
            feed_st = {}
        return {
            "version": VERSION, "mode": "live" if self.live else "dry",
            "state": state or ("halted" if self.halt_reason else "running"),
            "halt_reason": self.halt_reason, "variant": self.variant,
            "policy_overrides": self.policy_overrides, "subaccount": self.sub, "pid": os.getpid(),
            "quote_hours_et": self.P.get("quote_hours_et"),
            "started": C.iso(self.started), "now": C.iso(now), "rx": now,
            "loop": {"steps": self.steps, "decisions": self.decides, "p50_ms": q(lat, 0.5),
                     "p95_ms": q(lat, 0.95), "max_ms": q(lat, 1.0),
                     "wx_react_p50_ms": q(react, 0.5), "wx_react_max_ms": q(react, 1.0)},
            "wx": self.wx.status(now), "feed": feed_st, "events": ev_out,
            "orders_open": len(self.orders), "unconfirmed": len(self.unconfirmed),
            "positions": pos, "pnl_day": round(self.day_pnl(now), 2),
            "cash": None if self.cash is None else round(self.cash - self.cash_spent, 2),
            "guards": dict(self.guard_counts), "ops": dict(self.op_counts),
            "deferred": dict(self.deferred), "pulls": list(self.pulls)[-6:],
            "rest_last_minute": self.stats.last_minute(now), "rest_total": dict(self.stats.total),
            "errors_5min": len(self.errors), "halted_events": self.halted_events,
            "amend_expiry_ok": self.amend_expiry_ok,
            "writes": {"backoff_s": self.backoff_s,
                       "backoff_left_s": round(max(0.0, self.write_backoff_until - now), 2),
                       "unhealthy_s": (round(now - self.unhealthy_since, 1)
                                       if self.unhealthy_since is not None else None),
                       "transient": dict(self.backoff_counts)},
            "suspect": {t: round(now - s, 1) for t, s in self.suspect.items()},
            "ambiguous_creates": len(self.ambiguous), "confirm_hold": self.confirm_hold,
            "model": {"params": getattr(self.fair, "params_source", None),
                      "calibrations": getattr(self.fair, "cals_source", None),
                      "history_min": (round(self.fair.history_span_s() / 60.0, 1)
                                      if hasattr(self.fair, "history_span_s") else None)}}

    def _status(self, now: float, state: Optional[str] = None) -> None:
        try:
            write_json_atomic(os.path.join(self.dir, "status.json"), self.status(now, state))
        except Exception as e:                           # noqa: BLE001
            self.log(f"! status write failed: {e}", now)


# --------------------------------------------------------------------------
# Wiring and CLI
# --------------------------------------------------------------------------

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

    def release(self) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None


def parse_until(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    dt = _dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp()


def build_feed(ex: Exchange, cfg: Config, live: bool, log: Callable[[str], None]):
    import kalshi_ws
    prefix = CLIENT_PREFIX + "-"
    return kalshi_ws.KalshiFeed(
        WS_URL, ex.ws_headers(), log=log,
        fill_filter=lambda f: (str(f.get("client_order_id") or "").startswith(prefix)
                               or str(f.get("market_ticker") or "").startswith(SERIES + "-")),
        want_fills=live, want_order_groups=False, chunk=50, stale_secs=cfg.ws_stale_s,
        ping_secs=5.0)


def cancel_all_cli(ex: Exchange, sub: int, data_dir: Optional[str] = None,
                   out: Callable[[str], Any] = print) -> int:
    """--cancel-all: write HALT (a running bot cancels and idles), cancel
    every resting tmm- order in subaccount `sub`, read again and report what
    is left. Exit 0 when nothing of ours rests, 1 otherwise."""
    data_dir = data_dir or DATA_DIR
    hp = os.path.join(data_dir, "HALT")
    try:
        os.makedirs(data_dir, exist_ok=True)
        with open(hp, "a", encoding="utf-8") as f:
            f.write(f"{C.iso(time.time())} --cancel-all\n")
        out(f"HALT written ({hp}): a running bot cancels everything and idles; delete it to "
            f"let the bot quote again")
    except OSError as e:
        out(f"could not write {hp}: {e}")
    left: List[Tuple[str, str]] = []
    for attempt in range(4):
        try:
            rows = ex.resting()
        except ApiError as e:
            out(f"resting read failed: {e} {e.body[:120]}")
            return EXIT_ERROR
        items = [(str(r["order_id"]), str(r.get("ticker") or "")) for r in rows
                 if str(r.get("client_order_id") or "").startswith(CLIENT_PREFIX + "-")]
        if attempt == 0:
            out(f"{len(items)} resting tmm- order(s) in subaccount {sub}")
        left = items
        if not items or attempt == 3:
            break
        for oid, t in items:
            try:
                ex.cancel(oid, t)
                out(f"  cancelled {oid} {t}")
            except ApiError as e:
                out(f"  {oid} {t}: {e} {e.body[:120]}")
        time.sleep(0.5)
    if left:
        out(f"STILL RESTING after 3 rounds: {left} -- cancel them on kalshi.com")
        return EXIT_ERROR
    out(f"no tmm- order rests in subaccount {sub}")
    return EXIT_OK


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="send orders (default: dry run)")
    ap.add_argument("--subaccount", type=int, default=0,
                    help="Kalshi subaccount number (live needs N > 0)")
    ap.add_argument("--variant", default=None, help="F (default), G, J, ... or a VARIANTS key")
    ap.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    ap.add_argument("--until", default=None, help="stop at this time (ISO UTC or epoch)")
    ap.add_argument("--cancel-all", action="store_true",
                    help="cancel every resting tmm- order in the subaccount and exit (live)")
    ap.add_argument("--print-config", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="no console echo (the log file only)")
    args = ap.parse_args(argv)

    # our own buckets pace the writes; the shared client's fixed 100 ms gap
    # between calls would only slow an urgent cancel sequence (env wins)
    os.environ.setdefault("KALSHI_RATE_LIMIT_MS", "25")
    cfg = Config.from_env()
    try:
        variant = resolve_variant(args.variant or cfg.variant)
        over = policy_overrides_from_env()
        P = pol.make_policy(variant, **over)
        pol._check_policy(P)
    except ValueError as e:
        print(f"config error: {e}")
        return EXIT_REFUSED
    if args.print_config:
        print(json.dumps({"config": asdict(cfg), "variant": variant, "policy_overrides": over,
                          "policy": P, "data_dir": DATA_DIR, "api_base": API_BASE},
                         indent=1, default=str))
        return EXIT_OK

    mailer = send_email if (cfg.email and email_configured()) else None
    if args.live and args.subaccount <= 0:
        why = ("--live needs --subaccount N (N > 0): the bot trades its own numbered "
               "subaccount, never the primary. Create and fund one with kalshi_subaccount.py.")
        print(why)
        if mailer:
            mailer("Temp MM refused to start", why)
        return EXIT_REFUSED
    live = bool(args.live)
    os.makedirs(DATA_DIR, exist_ok=True)
    stats = CallStats()
    ex = Exchange(args.subaccount, live=live, stats=stats, read_rate=cfg.read_rate,
                  read_burst=cfg.read_burst, write_timeout_s=cfg.write_timeout_s,
                  read_timeout_s=cfg.read_timeout_s, read_attempts=cfg.read_attempts,
                  sync_read_timeout_s=cfg.sync_read_timeout_s)
    if args.cancel_all:
        # the emergency stop: BEFORE the single-instance lock (it must work
        # while a live bot -- hung or not -- holds it); HALT first, so a
        # running bot stops re-placing
        if not live:
            print("--cancel-all needs --live --subaccount N")
            return EXIT_REFUSED
        return cancel_all_cli(ex, args.subaccount)
    lock = SingleInstance(os.path.join(DATA_DIR, f"tmm_{'live' if live else 'dry'}.lock"))
    if not lock.acquire():
        print(f"another {'live' if live else 'dry-run'} temp MM holds the lock; exiting")
        return EXIT_REFUSED

    data_dir = DATA_DIR
    log_dir = data_dir if live else os.path.join(data_dir, "dry")
    feed_log = Log(log_dir, echo=not args.quiet, tag="TMM WS")
    feed = build_feed(ex, cfg, live, lambda s: feed_log(s))
    wx = WxSource(cfg, WxTail(), WxPoller(cfg.wx_poll_gap_s, stats, slow_gap_s=cfg.wx_slow_gap_s,
                                          yield_gap_s=cfg.wx_yield_gap_s, yield_s=cfg.wx_yield_s),
                  history_fetch=fetch_wx_history)
    fair = FairEngine(params_path=cfg.model_params, cals_path=cfg.calibrations,
                      min_span_s=cfg.min_wx_span_s)
    bot = Bot(cfg, P, variant, live, args.subaccount, ex, feed, wx, fair, data_dir=data_dir,
              threaded=True, mailer=mailer, echo=not args.quiet, policy_overrides=over)

    def on_signal(signum, _frame):
        bot.log(f"signal {signum}: stopping")
        bot.stop_evt.set()
        bot.wake.set()
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except (ValueError, OSError):
                pass
    atexit.register(lambda: bot.shutdown(time.time(), "atexit"))
    until = parse_until(args.until)
    try:
        code = bot.run(duration=args.duration, until=until)
    finally:
        lock.release()
    return code


if __name__ == "__main__":
    sys.exit(main())
