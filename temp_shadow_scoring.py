#!/usr/bin/env python3
"""Reward scoring + book/trade reconstruction for the hourly-temp shadow test.

Part of the KXTEMPMIAH shadow test (TEMP_SHADOW.md; Jack 2026-10-14: "build it
and shadow test for a week, reporting results and suggestion tweaks daily").
Never places an order and never touches the network.

1. Reward scoring -- an EXACT port of the IMM's post-2026-07-30 scorer
   (incentive_mm.py: levels_desc / side_reference_level / _side_share /
   estimate_reward_share, ~15027-15140), re-shaped for dict-of-cents books:

     reference_price(levels, target)          the level where cumulative size,
                                              best price first, reaches target/5
     side_share(levels, own, target, df)      levels INCLUDE own -> (share, qual)
     score(book_yes, book_no, own_yes, own_no, target, df)
                                              books EXCLUDE ours; we overlay ->
                                              (frac, counted, yes_qual, no_qual)
     would_cross(book_yes, book_no, side, price)

   test_temp_shadow_scoring.py extracts the ORIGINAL functions from
   incentive_mm.py with `ast` and checks bit-for-bit equality on thousands of
   random books plus every recorded study book.

2. EventStream / load_event_stream(event, t0, t1) -- rebuilds every strike's
   order book from the collector's ws recording (raw/<day>/ws_<HH>.jsonl[.gz],
   rows {"rx", "m"} and {"rx", "marker"}) and serves per-second states:

   * orderbook_snapshot REPLACES a book (yes_dollars_fp / no_dollars_fp
     [[price_dollars, size_fp]]; older yes_dollars, legacy yes/no [[cents,
     size]] accepted); orderbook_delta adds delta_fp at price_dollars on side
     yes|no (legacy price/delta accepted). Every level is kept in Kalshi's
     2-dp fixed point (rounded after each delta, so float sums never sit a
     hair under a target/5 or target boundary); sizes that round to 0 drop.
   * A book is TRUSTED only after its snapshot. connect / disconnect markers
     untrust every book until each gets a new snapshot; a gap marker untrusts
     the books on the sid it names (channel "trade" -> none; every book when
     it names neither sid nor markets); a resubscribe marker untrusts the
     markets it names. Sid maps reset at each connect (sids restart).
     Deltas on an untrusted book are ignored (the kalshi_ws rule). A
     stale-timer disconnect ("WSError: no frames for Ns") untrusts from
     N + 1.5 s before the marker (not before the previous row).
   * Recorder deaths. A connect with no disconnect since the previous connect
     = the old process died silently: books are untrusted retroactively from
     its last sign of life: its last ws row, or a later meta `status`
     heartbeat of the SAME process (pid + started of the last heartbeat at
     or before that row) reporting the ws connected, minus
     max(2 s, min(ws.last_msg_age_s, 35 s)). A heartbeat of any other process
     or one reporting the ws down ends the extension. A gap of
     > 150 s between heartbeats (every ~60 s), or heartbeats ending > 150 s
     before t1, blacks out every book over the gap (dead or stalled laptop;
     a stall's backlog then arrives complete, so trust resumes after it).
     A recording without heartbeats skips this rule.
   * Duplicate rows (an hour left in both .jsonl.gz and .jsonl by a crash
     inside the collector's gzip step, or appended to the .gz twice) are
     dropped: a row that goes back in time and matches an earlier row of
     the hour by (rx, type, sid, seq), or by its whole content for markers
     and seq-less rows.
   * books_per_second(sec0, sec1)[ticker][i] = (yes_levels, no_levels) as of
     rx <= sec0 + i + 0.5 (the random in-second scoring snapshot, taken at the
     middle), or None. Unchanged seconds share the SAME dict objects
     (copy-on-write); consumers must treat them as read-only.

3. Trades -- ws `trade` channel messages (live, per temp_shadow_collector's
   verified format: market_ticker, taker_side, yes_price_dollars, count_fp,
   ts s + ts_ms, trade_id, is_block_trade; legacy yes_price cents / count
   also parsed), merged with the REST trades in settled/<EVENT>.json: every REST
   trade whose trade_id the ws did not deliver is added, which fills exactly
   the spans where the ws was down (or the whole event when the ws recorded no
   trades). When both copies exist the ws one is kept (rx = our receive time)
   and the REST created_time (microseconds) replaces the ws ts_ms.
   ws trades without a trade_id are matched to REST ones fuzzily (ticker,
   side, price, count, |dt| <= 2 s). Block trades are skipped (they never
   touch the book). Trade.ts = exchange time, Trade.rx = receive time (= ts for
   REST). Sorted by ts; only t0 <= ts < t1 is kept.

4. coverage -- trusted fraction per ticker over [t0, t1) (sampled exactly like
   books_per_second), gaps / reconnects / silent deaths / recorder gaps,
   ws-down seconds, trade counts by source and trades_src ws|rest|mixed|none.

5. Live-order subtraction (2026-10-16; Jack: "run the sweep-aware shadow,
   without pausing"). From 2026-10-16 13:00Z the LIVE executor
   (temp_mm_live.py) rests its own orders in these markets, so the recorded
   books include them and a replay would count our size as competitors'.
   live_own_orders(event, sec0, sec1) rebuilds what we rested each second from
   the executor's logs (TEMP_MM_LIVE_DIR, else TEMP_MM_DATA, else
   KL-data\\temp-mm -- that default only while TEMP_SHADOW_DATA is the real
   recording, so a sandboxed replay never reads the real executor's logs:
   orders_<UTC day>.jsonl kinds place / adopt / amend / renew (total) /
   decrease (reduce_by) / cancel / gone / expired, fills_<UTC day>.jsonl
   reducing what is left; an order also leaves the book at its expiration_ts
   unless a renewal moved it; only rows with mode == "live"), and
   load_event_stream(..., subtract_live=None -> SUBTRACT_LIVE, default on,
   env TEMP_SHADOW_SUBTRACT_LIVE=0 turns it off) makes books_per_second /
   book_at return the recorded levels minus ours (clipped at 0) and drops the
   trades that filled our orders (by trade_id) from `trades` (the replayed
   bot gets the recorded market minus our own orders and fills; makers who
   reacted to our orders -- e.g. joined a 1c level our pad had opened --
   stay in it, so this is not a market that never saw us). With no live
   rows for the
   event it is a no-op: the same objects come back, so every pre-13:00Z
   replay is bit-identical. EventStream.live_own / own_trades / trades_all
   keep what was subtracted; coverage["live_own"] (only when rows exist)
   counts it.
"""
from __future__ import annotations

import bisect
import datetime as _dt
import json
import math
import os
import re
from collections import namedtuple
from typing import Dict, Iterable, List, Optional, Set, Tuple

import temp_shadow_common as C

TARGET = 1000.0      # target_size_fp of every KXTEMPMIAH program (10/14)
DF = 0.5             # discount_factor_bps 5000
EPS = 1e-9
SIZE_DP = 2          # Kalshi sizes are 2-decimal fixed point ("2.27"): replayed
                     # levels are rounded back to it so float deltas never leave
                     # a level at 199.9999999999999 instead of 200.00

# ==========================================================================
# 1. Reward scoring (exact port of incentive_mm, post-2026-07-30 rules)
# ==========================================================================


def _best_first(levels: Dict[int, float]) -> List[Tuple[int, float]]:
    """incentive_mm's sort: (price, size) highest price first. Prices in a
    dict are unique, so a plain reverse tuple sort is the same order as the
    original key=lambda kv: -kv[0], and faster."""
    return sorted(levels.items(), reverse=True)


def reference_price(levels: Dict[int, float], target: float = TARGET
                    ) -> Optional[int]:
    """side_reference_level(levels_desc(...)): the first level, best price
    first, where cumulative size reaches target/5; None if never (or target
    <= 0). `levels` are whatever book the caller wants referenced (the
    external book to place against, or one that includes us)."""
    if target <= 0:
        return None
    cum = 0.0
    for px, size in _best_first(levels):
        cum += size
        if cum >= target / 5.0:
            return px
    return None


def side_share(levels: Dict[int, float], own: Dict[int, float],
               target: float = TARGET, df: float = DF) -> Tuple[float, bool]:
    """incentive_mm._side_share on a dict book. `levels` INCLUDE our own size;
    `own` = price -> our size. Returns (our_share, side_qualifies)."""
    levels_best_first = _best_first(levels)
    # ---- verbatim body of incentive_mm._side_share ----
    if not levels_best_first or target <= 0:
        return 0.0, False
    ref: Optional[int] = None
    accumulated = 0.0
    qualifying: List[Tuple[int, float]] = []
    for px, size in levels_best_first:
        qualifying.append((px, size))
        accumulated += size
        if ref is None and accumulated >= target / 5.0:
            ref = px
        if accumulated >= target:
            break
    else:
        return 0.0, False   # book thinner than target: side doesn't qualify
    total_score = 0.0
    our_score = 0.0
    for px, size in qualifying:
        w = df ** max(ref - px, 0)
        total_score += w * size
        our_score += w * min(own.get(px, 0.0), size)
    if total_score <= 0:
        return 0.0, False
    return our_score / total_score, True


def overlay(book: Dict[int, float], own: Optional[Dict[int, float]]
            ) -> Dict[int, float]:
    """book + own (estimate_reward_share's own_in_book=False prep). Returns
    `book` itself (no copy) when own is empty; never mutates its inputs."""
    if not own:
        return book
    by_px = dict(book)
    for px, q in own.items():
        by_px[px] = by_px.get(px, 0.0) + q
    return by_px


def score(book_yes: Dict[int, float], book_no: Dict[int, float],
          own_yes: Optional[Dict[int, float]] = None,
          own_no: Optional[Dict[int, float]] = None,
          target: float = TARGET, df: float = DF
          ) -> Tuple[float, bool, bool, bool]:
    """One scoring snapshot. book_* EXCLUDE our orders (we overlay own_*;
    NO-side prices are NO-bid cents). Returns (frac, counted, yes_qual,
    no_qual): frac = (s_yes + s_no) / 2 when BOTH sides reach target, else
    0.0 (the snapshot is excluded and pays nobody). Same numbers as
    incentive_mm.estimate_reward_share(..., own_in_book=False)."""
    own_yes = own_yes or {}
    own_no = own_no or {}
    s_yes, q_yes = side_share(overlay(book_yes or {}, own_yes), own_yes,
                              target, df)
    s_no, q_no = side_share(overlay(book_no or {}, own_no), own_no,
                            target, df)
    if not (q_yes and q_no):
        return 0.0, False, q_yes, q_no
    return (s_yes + s_no) / 2.0, True, q_yes, q_no


def share(book_yes, book_no, own_yes=None, own_no=None,
          target: float = TARGET, df: float = DF) -> float:
    """Convenience: score(...)[0]."""
    return score(book_yes, book_no, own_yes, own_no, target, df)[0]


def side_qualifies(levels: Dict[int, float], target: float = TARGET) -> bool:
    """Does this side reach target (the walk's qualification test)?"""
    return target > 0 and sum(q for q in levels.values()) >= target


def best_price(levels: Optional[Dict[int, float]]) -> Optional[int]:
    """Best (highest) price with size > 1e-9, or None."""
    if not levels:
        return None
    best = None
    for px, q in levels.items():
        if q > EPS and (best is None or px > best):
            best = px
    return best


def would_cross(book_yes: Dict[int, float], book_no: Dict[int, float],
                side: str, price: int) -> bool:
    """Would a post-only bid cross the opposite book? side 'yes' (a YES bid
    at `price`) crosses iff price + best NO bid >= 100; side 'no' (a NO bid
    at `price`) iff price + best YES bid >= 100. An empty opposite side never
    crosses. 'yes_bid' / 'no_bid' are accepted as aliases."""
    s = side.lower()
    if s in ("yes", "yes_bid"):
        opp = best_price(book_no)
    elif s in ("no", "no_bid"):
        opp = best_price(book_yes)
    else:
        raise ValueError(f"side must be 'yes' or 'no', not {side!r}")
    return opp is not None and price + opp >= 100


def book_from_rest(orderbook: dict) -> Tuple[Dict[int, float], Dict[int, float]]:
    """A REST get_orderbook body ({"orderbook_fp": {"yes_dollars", "no_dollars"}}
    or the inner dict) -> ({cents: size} YES bids, {cents: size} NO bids)."""
    ob = (orderbook or {}).get("orderbook_fp", orderbook) or {}
    return (C.levels_from_fp(ob.get("yes_dollars") or ob.get("yes_dollars_fp")),
            C.levels_from_fp(ob.get("no_dollars") or ob.get("no_dollars_fp")))


# ==========================================================================
# 2./3. Event stream: books + trades from the recording
# ==========================================================================

Trade = namedtuple("Trade", "ts rx ticker taker_side yes_cents count")
# taker_side 'yes' = the taker BOUGHT YES (lifted YES asks = NO bids)

LOOKBACK_S = 10800.0     # read this far before t0 for each market's snapshot.
# 3 h, not 1 h (2026-10-15): when Kalshi skips hours (no 04/05 ET events on
# 10/15) the next event lists ~2 h early, and a WS reconnect in that span
# re-snapshots it 70 min before open -- a 1 h read missed the snapshot and the
# whole 26OCT1506 hour replayed as untrusted.
TRADE_SLACK_S = 10.0     # read ws rows this far past t1 (late-received trades)
FUZZY_TRADE_S = 2.0      # ws trade without trade_id <-> REST match tolerance
TAIL_SILENCE_S = 120.0   # no ws row this long before t1 -> report ws down
                         # (only when the recording has no status heartbeats)
STATUS_GAP_S = 150.0     # meta 'status' heartbeats come every ~60 s: a longer
                         # gap = the recorder was dead or stalled
LIFE_MARGIN_S = 2.0      # writer flushes <= 1 s: last heartbeat - 2 s is the
                         # last moment the recording is surely complete
STALE_GUARD_S = 35.0     # the collector drops a connection after 30 s with no
                         # frame (pongs to its 10 s pings count), checked about
                         # once a second: a process that reports `connected`
                         # surely had a live link within the last 35 s
STALE_SLACK_S = 1.5      # "no frames for Ns": N is rounded to the second and
                         # the marker is written after the socket close
_NO_FRAMES_RE = re.compile(r"no frames for (\d+(?:\.\d+)?)\s*s")

# op codes in the per-ticker timelines
_SNAP, _DELTA, _UNTRUST = 0, 1, 2


def _cents_dollars(px) -> Optional[int]:
    try:
        return C.dollars_to_cents(px)
    except (TypeError, ValueError):
        return None


def _cents_legacy(px) -> Optional[int]:
    """Legacy integer-cents field (yes_price, price, [[cents, size]]). A
    string with a '.' is a dollar string sent under the old name."""
    try:
        if isinstance(px, str) and "." in px:
            return C.dollars_to_cents(px)
        return int(round(float(px)))
    except (TypeError, ValueError):
        return None


def _levels_cents(rows) -> Dict[int, float]:
    """Legacy [[cents, size], ...] -> {cents: size} (sizes summed)."""
    out: Dict[int, float] = {}
    for row in rows or []:
        try:
            c, s = _cents_legacy(row[0]), C.fp(row[1])
        except (TypeError, IndexError):
            continue
        if c is not None and s > EPS:
            out[c] = out.get(c, 0.0) + s
    return out


def _fixed_point(levels: Dict[int, float]) -> Dict[int, float]:
    """Sizes rounded to Kalshi's 2-dp fixed point; levels that round to 0
    dropped."""
    out: Dict[int, float] = {}
    for px, q in levels.items():
        q = round(q, SIZE_DP)
        if q > EPS:
            out[px] = q
    return out


def snapshot_levels(body: dict) -> Tuple[Dict[int, float], Dict[int, float]]:
    """orderbook_snapshot msg body -> (yes {cents: size}, no {cents: size}),
    sizes in 2-dp fixed point."""
    def side(name: str) -> Dict[int, float]:
        rows = body.get(f"{name}_dollars_fp")
        if rows is None:
            rows = body.get(f"{name}_dollars")
        if rows is not None:
            return _fixed_point(C.levels_from_fp(rows))
        return _fixed_point(_levels_cents(body.get(name)))
    return side("yes"), side("no")


def delta_fields(body: dict) -> Optional[Tuple[bool, int, float]]:
    """orderbook_delta msg body -> (is_yes, cents, delta) or None."""
    side = str(body.get("side") or "").lower()
    if side not in ("yes", "no"):
        return None
    if body.get("price_dollars") is not None:
        c = _cents_dollars(body.get("price_dollars"))
    else:
        c = _cents_legacy(body.get("price"))
    d = body.get("delta_fp")
    if d is None:
        d = body.get("delta")
    if c is None or d is None:
        return None
    return side == "yes", c, C.fp(d)


def _num_ts(v) -> Optional[float]:
    """Exchange timestamp in s from seconds / ms / ISO."""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        try:
            v = float(v)
        except ValueError:
            return C.parse_iso(v)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v > 1e11:          # milliseconds
        v /= 1000.0
    return v


def _trade_side(body: dict) -> Optional[str]:
    s = str(body.get("taker_side") or body.get("taker_outcome_side") or "").lower()
    return s if s in ("yes", "no") else None


def _trade_yes_cents(body: dict) -> Optional[int]:
    if body.get("yes_price_dollars") not in (None, ""):
        return _cents_dollars(body["yes_price_dollars"])
    if body.get("yes_price") not in (None, ""):
        return _cents_legacy(body["yes_price"])
    if body.get("no_price_dollars") not in (None, ""):
        c = _cents_dollars(body["no_price_dollars"])
        return None if c is None else 100 - c
    if body.get("no_price") not in (None, ""):
        c = _cents_legacy(body["no_price"])
        return None if c is None else 100 - c
    return None


def _trade_count(body: dict) -> float:
    v = body.get("count_fp")
    if v in (None, ""):
        v = body.get("count")
    return C.fp(v)


def parse_ws_trade(body: dict, rx: float) -> Optional[Tuple[Trade, Optional[str]]]:
    """ws trade msg body -> (Trade, trade_id). The live format (verified by
    temp_shadow_collector 2026-10-15: yes_price_dollars, count_fp, ts epoch s,
    ts_ms epoch ms, trade_id, is_block_trade) and the documented legacy one
    (yes_price cents, count). ts_ms is preferred, then ts (s, or ms if huge),
    then ISO created_time; rx when none. Block trades -> None."""
    if body.get("is_block_trade"):
        return None
    tk = body.get("market_ticker") or body.get("ticker")
    side = _trade_side(body)
    yc = _trade_yes_cents(body)
    cnt = _trade_count(body)
    if not tk or side is None or yc is None or cnt <= EPS:
        return None
    ts = None
    for key in ("ts_ms", "ts", "created_time", "time"):
        ts = _num_ts(body.get(key))
        if ts is not None:
            break
    if ts is None:
        ts = rx
    return Trade(ts, rx, tk, side, yc, cnt), (body.get("trade_id") or None)


def parse_rest_trade(r: dict, ticker: Optional[str] = None
                     ) -> Optional[Tuple[Trade, Optional[str]]]:
    """REST /markets/trades row -> (Trade with rx = ts, trade_id). Block
    trades -> None."""
    if r.get("is_block_trade"):
        return None
    tk = r.get("ticker") or ticker
    side = _trade_side(r)
    yc = _trade_yes_cents(r)
    cnt = _trade_count(r)
    ts = _num_ts(r.get("created_time"))
    if ts is None:
        ts = _num_ts(r.get("ts"))
    if not tk or side is None or yc is None or cnt <= EPS or ts is None:
        return None
    return Trade(ts, ts, tk, side, yc, cnt), (r.get("trade_id") or None)


def _marker_list(rec: dict, *keys) -> List:
    out: List = []
    for k in keys:
        v = rec.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            out.extend(v)
        else:
            out.append(v)
    return out


def _strike_key(t: str):
    s = C.strike_of(t)
    return (s if s is not None else math.inf, t)


# ==========================================================================
# 5. Live-order reconstruction (our own orders inside the recorded books)
# ==========================================================================

LIVE_DIR_DEFAULT = r"C:\Users\jackd\Documents\KL-data\temp-mm"
SUBTRACT_LIVE = os.environ.get("TEMP_SHADOW_SUBTRACT_LIVE", "1").strip().lower() \
    not in ("0", "false", "no", "off")
LIVE_READ_BEFORE_S = 6 * 3600.0   # executor log days read around a window
LIVE_READ_AFTER_S = 3600.0
# order-log kinds that change what rests (the rest -- reject, uncertain,
# *_error, cancel_elsewhere (another subaccount), pull/restore -- do not)
_LIVE_KINDS = {"place": 0, "adopt": 0, "amend": 1, "renew": 1, "decrease": 1,
               "fill": 2, "cancel": 3, "gone": 3, "expired": 3}
_LIVE_CACHE: Dict[str, tuple] = {}     # path -> (mtime, size, live rows)


DATA_ROOT_DEFAULT = r"C:\Users\jackd\Documents\KL-data\temp-shadow"


def live_dir() -> Optional[str]:
    """The live executor's data dir, read per call: env TEMP_MM_LIVE_DIR,
    else env TEMP_MM_DATA (where temp_mm_live itself writes when relocated),
    else KL-data\\temp-mm -- but only for the real recording: when
    TEMP_SHADOW_DATA points elsewhere (a test sandbox, a copy) and neither
    env names a live dir, None (nothing is subtracted), so a sandboxed replay
    never reads the real executor's logs."""
    d = os.environ.get("TEMP_MM_LIVE_DIR") or os.environ.get("TEMP_MM_DATA")
    if d:
        return d
    if os.path.normcase(os.path.realpath(C.DATA_ROOT)) != \
            os.path.normcase(os.path.realpath(DATA_ROOT_DEFAULT)):
        return None
    return LIVE_DIR_DEFAULT


def _live_file_rows(path: str) -> List[dict]:
    """mode == "live" rows of one executor log, cached by (mtime, size)."""
    try:
        st = os.stat(path)
    except OSError:
        return []
    hit = _LIVE_CACHE.get(path)
    if hit is not None and hit[0] == st.st_mtime and hit[1] == st.st_size:
        return hit[2]
    rows = [r for r in C.iter_jsonl(path) if r.get("mode") == "live"]
    _LIVE_CACHE[path] = (st.st_mtime, st.st_size, rows)
    return rows


def live_log_rows(kind: str, t0: float, t1: float, root: Optional[str] = None
                  ) -> List[dict]:
    """Live rows of the executor's `<kind>_<UTC day>.jsonl` (kind 'orders' or
    'fills') for every UTC day touching [t0 - 6 h, t1 + 1 h]."""
    root = root or live_dir()
    if not root or not os.path.isdir(root):
        return []
    out: List[dict] = []
    d = _dt.datetime.fromtimestamp(t0 - LIVE_READ_BEFORE_S, tz=_dt.timezone.utc).date()
    end = _dt.datetime.fromtimestamp(t1 + LIVE_READ_AFTER_S, tz=_dt.timezone.utc).date()
    while d <= end:
        out.extend(_live_file_rows(os.path.join(root, f"{kind}_{d.isoformat()}.jsonl")))
        d += _dt.timedelta(days=1)
    return out


class LiveOrders:
    """Our live orders on one event, rebuilt from the executor's logs.

    orders   oid -> {ticker, side ('yes' | 'no' = YES bid | NO bid), price,
             placed (ts), tag} for every order that rested
    fills    the event's live fill rows (ts, order_id, count, trade_id, ...)
    ops      [(ts, prio, oid, kind, row)] time-ordered state changes
    trade_ids  trade ids of our fills (the tape entries that hit us)
    """

    def __init__(self, event: str, orders: List[dict], fills: List[dict]):
        self.event = event
        prefix = event + "-"
        self.orders: Dict[str, dict] = {}
        self.fills: List[dict] = []
        ops: List[tuple] = []
        for r in orders:
            k = r.get("kind")
            tk = str(r.get("ticker") or "")
            oid = r.get("oid")
            if k not in _LIVE_KINDS or k == "fill" or not oid or not tk.startswith(prefix):
                continue
            try:
                ts = float(r["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if k in ("place", "adopt") and oid not in self.orders:
                try:
                    self.orders[oid] = dict(ticker=tk, side=str(r.get("side")).lower(),
                                            price=int(r["price"]), placed=ts,
                                            tag=r.get("tag"))
                except (KeyError, TypeError, ValueError):
                    continue
            ops.append((ts, _LIVE_KINDS[k], oid, k, r))
        tids: Set[str] = set()
        for f in fills:
            tk = str(f.get("ticker") or "")
            oid = f.get("order_id")
            if not oid or not tk.startswith(prefix):
                continue
            try:
                ts = float(f["ts"])
                cnt = float(f.get("count") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            if cnt <= EPS:
                continue
            self.fills.append(f)
            if f.get("trade_id"):
                tids.add(str(f["trade_id"]))
            ops.append((ts, _LIVE_KINDS["fill"], oid, "fill", f))
        ops.sort(key=lambda o: (o[0], o[1]))
        self.ops = ops
        self.trade_ids = tids

    def __bool__(self) -> bool:
        return bool(self.orders)

    def timeline(self, sec0: int, sec1: int,
                 sample_offset: float = 0.5) -> List[Dict[str, Tuple[Dict[int, float], Dict[int, float]]]]:
        """Entry i = {ticker: (own_yes, own_no)} resting as of
        sec0 + i + sample_offset (books_per_second's sampling); tickers with
        nothing resting are omitted. Unchanged seconds share the SAME dict
        objects (read-only)."""
        n = max(0, int(sec1) - int(sec0))
        out: List[dict] = [None] * n
        st: Dict[str, list] = {}          # oid -> [price, total, expires]
        filled: Dict[str, float] = {}
        ops = self.ops
        j = 0
        cur: dict = {}
        dirty = True
        next_exp = math.inf
        for i in range(n):
            b = sec0 + i + sample_offset
            while j < len(ops) and ops[j][0] <= b:
                ts, _, oid, k, r = ops[j]
                j += 1
                dirty = True
                if k == "fill":
                    filled[oid] = filled.get(oid, 0.0) + float(r.get("count") or 0.0)
                    continue
                if k in ("place", "adopt"):
                    if oid not in self.orders:
                        continue
                    try:
                        size = float(r.get("size") or 0.0)
                    except (TypeError, ValueError):
                        size = 0.0
                    exp = r.get("expiration_ts")
                    exp = float(exp) if exp not in (None, "") else math.inf
                    total = size if k == "place" else filled.get(oid, 0.0) + size
                    st[oid] = [int(r.get("price", self.orders[oid]["price"])), total, exp]
                    continue
                o = st.get(oid)
                if o is None:
                    continue
                if k in ("amend", "renew"):
                    try:
                        if r.get("price") is not None:
                            o[0] = int(r["price"])
                        if r.get("total") not in (None, ""):
                            o[1] = float(r["total"])
                        elif r.get("size") not in (None, ""):
                            o[1] = filled.get(oid, 0.0) + float(r["size"])
                    except (TypeError, ValueError):
                        pass
                    if r.get("expiration_ts") not in (None, ""):
                        o[2] = float(r["expiration_ts"])
                elif k == "decrease":
                    try:
                        if r.get("reduce_by") not in (None, ""):
                            o[1] -= float(r["reduce_by"])
                        elif r.get("size") not in (None, ""):
                            o[1] = filled.get(oid, 0.0) + float(r["size"])
                    except (TypeError, ValueError):
                        pass
                else:                                  # cancel / gone / expired
                    st.pop(oid, None)
            if b >= next_exp:
                dirty = True
            if dirty:
                cur = {}
                next_exp = math.inf
                for oid, o in list(st.items()):
                    if o[2] <= b:                      # Kalshi expired it
                        st.pop(oid, None)
                        continue
                    left = o[1] - filled.get(oid, 0.0)
                    if left <= EPS:
                        continue
                    next_exp = min(next_exp, o[2])
                    meta = self.orders[oid]
                    pair = cur.get(meta["ticker"])
                    if pair is None:
                        pair = cur[meta["ticker"]] = ({}, {})
                    d = pair[0] if meta["side"] == "yes" else pair[1]
                    d[o[0]] = round(d.get(o[0], 0.0) + left, SIZE_DP)
                dirty = False
            out[i] = cur
        return out


def load_live_orders(event_ticker: str, t0: Optional[float] = None,
                     t1: Optional[float] = None, root: Optional[str] = None
                     ) -> Optional[LiveOrders]:
    """The event's live orders (None when the executor logged none for it).
    t0/t1 default to the ticker's hour."""
    if t0 is None or t1 is None:
        pe = C.parse_event(event_ticker)
        if not pe:
            return None
        t0, t1 = pe[1] - 3600.0, pe[1]
    orders = live_log_rows("orders", t0, t1, root)
    if not orders:
        return None
    lo = LiveOrders(event_ticker, orders, live_log_rows("fills", t0, t1, root))
    return lo if lo else None


def live_own_orders(event_ticker: str, sec0: int, sec1: int,
                    root: Optional[str] = None
                    ) -> Optional[List[Dict[str, Tuple[Dict[int, float], Dict[int, float]]]]]:
    """Our live resting orders per second: entry i = {ticker: (own_yes,
    own_no)} ({cents: contracts} YES bids / NO bids) as of sec0 + i + 0.5,
    the books_per_second sampling; tickers with nothing resting are omitted.
    None when the live executor logged no order for the event (the
    subtraction is then a no-op)."""
    lo = load_live_orders(event_ticker, float(sec0), float(sec1), root)
    if lo is None:
        return None
    return lo.timeline(int(sec0), int(sec1))


def subtract_levels(levels: Dict[int, float], own: Optional[Dict[int, float]]
                    ) -> Dict[int, float]:
    """levels minus own per price, clipped at 0 (2-dp fixed point; levels
    left <= 1e-9 dropped). Returns `levels` itself when own is empty."""
    if not own:
        return levels
    out: Dict[int, float] = {}
    for px, q in levels.items():
        left = round(q - own.get(px, 0.0), SIZE_DP)
        if left > EPS:
            out[px] = left
    return out


class EventStream:
    """Books, trades and coverage of one event over [t0, t1) (see module doc).

    tickers   strike tickers, ascending strike
    trades    List[Trade] sorted by ts (ws + REST fill-in)
    coverage  dict (trusted_frac per ticker, book_trusted_frac, gaps,
              reconnects, disconnects, silent_deaths, ws_down_secs,
              trades / trades_ws / trades_rest / trades_src, ...)
    markets   ticker -> latest REST market object (meta 'markets' rows, then
              the settled file), may be empty
    settled   the settled/<EVENT>.json dict or None
    trade_info  list parallel to trades: (source 'ws'|'rest', trade_id)
    """

    def __init__(self, event_ticker: str, t0: float, t1: float):
        self.event = event_ticker
        self.t0 = float(t0)
        self.t1 = float(t1)
        self.tickers: List[str] = []
        self.trades: List[Trade] = []
        self.trade_info: List[Tuple[str, Optional[str]]] = []
        self.coverage: dict = {}
        self.markets: Dict[str, dict] = {}
        self.settled: Optional[dict] = None
        self._ops: Dict[str, List[tuple]] = {}
        self._down: List[Tuple[float, float]] = []   # ws-down spans
        # recorder dead/stalled spans (from status heartbeats): every book
        # reads None inside them; deltas keep applying (a stall's backlog
        # arrives complete afterwards; a restart's connect marker untrusts)
        self._blackout: List[Tuple[float, float]] = []
        # live-order subtraction (module doc 5): set by load_event_stream
        self.subtract_live: bool = SUBTRACT_LIVE
        self.live_root: Optional[str] = None
        self.live_own: Optional[LiveOrders] = None   # None = nothing to subtract
        self.own_trades: List[Trade] = []            # tape rows that filled us (removed)
        self.trades_all: Optional[List[Trade]] = None  # the tape before removal

    # ---------------------------------------------------------------- books
    def books_per_second(self, sec0: int, sec1: int
                         ) -> Dict[str, List[Optional[Tuple[Dict[int, float],
                                                             Dict[int, float]]]]]:
        """{ticker: [state for sec0 .. sec1-1]}; entry i = (yes, no) as of
        rx <= sec0 + i + 0.5, None when untrusted / unknown. Unchanged
        seconds share one tuple and its dicts: treat them as READ-ONLY.
        With live-order subtraction on (and live orders logged for the
        event) our own resting sizes are taken out of every level."""
        sec0, sec1 = int(sec0), int(sec1)
        out = {t: self._replay(t, sec0, sec1, True) for t in self.tickers}
        if self.subtract_live and self.live_own:
            own = self.live_own.timeline(sec0, sec1)
            out = {t: _subtract_series(lst, own, t) for t, lst in out.items()}
        return out

    def book_at(self, ticker: str, ts: float
                ) -> Optional[Tuple[Dict[int, float], Dict[int, float]]]:
        """State as of rx <= ts (fresh copies; ours subtracted like
        books_per_second)."""
        st = self._replay(ticker, 0, 1, True, first_boundary=ts)[0]
        if st is None:
            return None
        if self.subtract_live and self.live_own:
            s0 = math.floor(ts)
            own = self.live_own.timeline(s0, s0 + 1, sample_offset=ts - s0)[0].get(ticker)
            if own:
                st = (subtract_levels(st[0], own[0]), subtract_levels(st[1], own[1]))
        return (dict(st[0]), dict(st[1]))

    def _replay(self, ticker: str, sec0: int, sec1: int, materialize: bool,
                first_boundary: Optional[float] = None) -> list:
        """Walk the ticker's op timeline once, sampling at sec + 0.5.
        materialize=False returns booleans (trusted?) instead of states."""
        n = max(0, sec1 - sec0)
        res: list = [None] * n if materialize else [False] * n
        ops = self._ops.get(ticker) or []
        nops = len(ops)
        bo = self._blackout
        nbo = len(bo)
        k = 0
        j = 0
        trusted = False
        yes = no = None
        yes_shared = no_shared = True
        cur = None
        changed = True
        for i in range(n):
            b = (first_boundary if first_boundary is not None
                 else sec0 + i + 0.5)
            while j < nops and ops[j][0] <= b:
                op = ops[j]
                j += 1
                kind = op[1]
                if kind == _SNAP:
                    yes, no = op[2], op[3]          # owned by the op: COW
                    yes_shared = no_shared = True
                    trusted = True
                    changed = True
                elif kind == _DELTA:
                    if not trusted:
                        continue                    # wait for a snapshot
                    is_yes, px, d = op[2], op[3], op[4]
                    # back to the exchange's 2-dp fixed point after every
                    # delta: float sums of "721.93" + "307.15" - "829.08"
                    # would read 199.9999999999999, not 200.00, and move
                    # the reference / qualification at target/5 or target
                    if is_yes:
                        if yes_shared:
                            yes = dict(yes)
                            yes_shared = False
                        q = round(yes.get(px, 0.0) + d, SIZE_DP)
                        if q > EPS:
                            yes[px] = q
                        else:
                            yes.pop(px, None)
                    else:
                        if no_shared:
                            no = dict(no)
                            no_shared = False
                        q = round(no.get(px, 0.0) + d, SIZE_DP)
                        if q > EPS:
                            no[px] = q
                        else:
                            no.pop(px, None)
                    changed = True
                else:                               # _UNTRUST
                    trusted = False
                    yes = no = None
                    cur = None
                    changed = True
            if not trusted:
                continue
            if nbo:                                 # spans sorted + merged
                while k < nbo and bo[k][1] <= b:
                    k += 1
                if k < nbo and bo[k][0] <= b:
                    continue                        # recorder dead / stalled
            if not materialize:
                res[i] = True
                continue
            if changed:
                cur = (yes, no)
                yes_shared = no_shared = True
                changed = False
            res[i] = cur
        return res

    def trusted_frac(self, ticker: str, sec0: int, sec1: int) -> float:
        n = max(0, int(sec1) - int(sec0))
        if n == 0:
            return 0.0
        return sum(self._replay(ticker, int(sec0), int(sec1), False)) / n

    # --------------------------------------------------------------- loading
    def _load(self, lookback: float, settled_path: Optional[str]) -> None:
        ev = self.event
        prefix = ev + "-"
        lo = self.t0 - lookback
        hi = self.t1 + TRADE_SLACK_S
        win0, win1 = self.t0, self.t1

        # ---- meta: market objects + recorder heartbeats -----------------
        # (rx, (pid, started), connected, ws last_msg_age_s); `started` in
        # the identity guards against Windows reusing a dead process's pid
        status: List[Tuple[float, tuple, object, Optional[float]]] = []
        for rec in C.iter_raw("meta", lo, hi):
            kind = rec.get("kind")
            if kind == "status":
                d = rec.get("data") if isinstance(rec.get("data"), dict) else {}
                wsd = d.get("ws") if isinstance(d.get("ws"), dict) else {}
                conn_flag = d.get("connected", wsd.get("connected"))
                try:
                    age = wsd.get("last_msg_age_s")
                    age = None if age is None else float(age)
                except (TypeError, ValueError):
                    age = None
                try:
                    status.append((float(rec["rx"]),
                                   (d.get("pid"), d.get("started")),
                                   conn_flag, age))
                except (TypeError, ValueError, KeyError):
                    pass
                continue
            if kind != "markets":
                continue
            data = rec.get("data") or {}
            if data.get("event_ticker") != ev:
                continue
            for m in data.get("markets") or []:
                t = m.get("ticker")
                if t and t.startswith(prefix):
                    self.markets[t] = m

        # ---- ws ----------------------------------------------------------
        ops: Dict[str, List[tuple]] = {}
        seen: Set[str] = set()
        sid_markets: Dict[int, Set[str]] = {}    # book sid -> OUR markets
        book_sids: Set[int] = set()              # every orderbook sid seen
        trade_sids: Set[int] = set()
        sid_seq: Dict[int, int] = {}
        ws_trades: List[Tuple[Trade, Optional[str]]] = []
        last_rx: Optional[float] = None          # last ws row of any kind
        first_rx: Optional[float] = None
        conn = "unknown"                         # unknown | up | down
        down_start: Optional[float] = None
        down: List[Tuple[float, float]] = []
        cnt = {"gaps": 0, "reconnects": 0, "disconnects": 0,
               "silent_deaths": 0, "resubscribes": 0, "ws_rows": 0,
               "snapshots": 0, "deltas": 0, "seq_anomalies": 0,
               "ws_trades_raw": 0, "dup_rows": 0}

        def in_win(rx: float) -> bool:
            return win0 <= rx < win1

        def untrust(tickers: Iterable[str], rx: float) -> None:
            for t in tickers:
                lst = ops.setdefault(t, [])
                if lst and lst[-1][1] == _UNTRUST:
                    continue
                lst.append((rx, _UNTRUST))

        def all_ours() -> Set[str]:
            return set(ops) | seen

        status.sort(key=lambda r: r[0])
        status_rx = [r[0] for r in status]

        def death_start(last: float, c_rx: float) -> float:
            """When did the recorder that wrote `last` (its last ws row) die,
            given a connect at c_rx with no disconnect before it?

            Only heartbeats of THAT process count: its identity (pid,
            started) is the one of the last heartbeat at or before `last`
            (a single instance runs at a time, so no other process can be
            alive then). Each of its later heartbeats that reports the ws
            connected proves the recording complete up to
            hb - max(LIFE_MARGIN_S, min(last_msg_age_s, STALE_GUARD_S)):
            its last text frame (hb - age) or, on a quiet link, the
            stale-timer bound. The walk stops at the first heartbeat of
            another process (one that started, maybe never connected, and
            exited) or one reporting the ws down."""
            t = last
            k = bisect.bisect_right(status_rx, last)
            if k > 0 and status[k - 1][1][0] is not None:
                ident = status[k - 1][1]
                kk = bisect.bisect_left(status_rx, c_rx)
                for r, idt, connected, age in status[k:kk]:
                    if idt != ident or connected is not True:
                        break
                    back = (STALE_GUARD_S if age is None
                            else min(age, STALE_GUARD_S))
                    t = max(t, r - max(LIFE_MARGIN_S, back))
            return min(t + 1e-6, c_rx)

        row_keys: Set[tuple] = set()
        key_hour: Optional[int] = None
        for rec in C.iter_raw("ws", lo, hi):
            rx = float(rec.get("rx"))
            # A crash between the collector's gzip os.replace and os.remove
            # leaves an hour's rows in BOTH .jsonl.gz and .jsonl (and the
            # restart's gzip step then appends them to the .gz a second
            # time): a row that goes back in time and was already seen is
            # that duplicate. Exchange messages are keyed by (rx, type, sid,
            # seq); markers and seq-less rows by their whole content, since
            # the coarse clock gives distinct rows the same rx (795 of 4,024
            # live rows) and a trade-sid gap marker must not swallow the
            # book-sid one written in the same tick.
            mm = rec.get("m")
            if isinstance(mm, dict) and mm.get("seq") is not None:
                key = (rx, mm.get("type"), mm.get("sid"), mm.get("seq"))
            else:
                key = (rx, json.dumps(rec, sort_keys=True, default=str))
            hr = int(rx // 3600)                 # duplicates stay in their
            if key_hour is None or hr > key_hour:  # own hour file
                row_keys.clear()
                key_hour = hr
            if last_rx is not None and rx <= last_rx and key in row_keys:
                cnt["dup_rows"] += 1
                continue
            row_keys.add(key)
            if last_rx is not None and rx < last_rx:
                rx = last_rx                     # keep every timeline sorted
            if first_rx is None:
                first_rx = rx
            marker = rec.get("marker")
            if marker is not None:
                marker = str(marker).lower()
                if marker == "connect":
                    if conn != "down" and last_rx is not None:
                        # no disconnect since the last connect: the recorder
                        # died silently after its last row
                        ds = death_start(last_rx, rx)
                        untrust(all_ours(), ds)
                        down.append((ds, rx))
                        if in_win(rx):
                            cnt["silent_deaths"] += 1
                    elif conn == "down" and down_start is not None:
                        down.append((down_start, rx))
                    elif conn == "unknown" and last_rx is None and rx > lo:
                        down.append((lo, rx))    # recorder started in range
                    untrust(all_ours(), rx)
                    conn = "up"
                    down_start = None
                    sid_seq.clear()             # sids restart per connection
                    sid_markets.clear()
                    book_sids.clear()
                    trade_sids.clear()
                    if in_win(rx):
                        cnt["reconnects"] += 1
                elif marker == "disconnect":
                    # the stale timer's teardown ("WSError: no frames for
                    # 31s") says the link was already dead N s before the
                    # marker: untrust from then (never before the previous
                    # row, which proves the link alive at its rx)
                    at = rx
                    mf = _NO_FRAMES_RE.search(str(rec.get("detail") or ""))
                    if mf:
                        at = rx - float(mf.group(1)) - STALE_SLACK_S
                        if last_rx is not None:
                            at = max(last_rx + 1e-6, at)
                        at = min(rx, at)
                    untrust(all_ours(), at)
                    if conn != "down":
                        down_start = at
                    conn = "down"
                    sid_seq.clear()
                    if in_win(rx):
                        cnt["disconnects"] += 1
                elif marker == "gap":
                    named = [t for t in _marker_list(
                        rec, "markets", "market_tickers", "tickers",
                        "market_ticker", "ticker") if isinstance(t, str)]
                    sids = []
                    for s in _marker_list(rec, "sid", "sids"):
                        try:
                            sids.append(int(s))
                        except (TypeError, ValueError):
                            pass
                    if not named and not sids:
                        m = re.search(r"\bsid\D{0,3}(\d+)",
                                      str(rec.get("detail") or ""))
                        if m:
                            sids.append(int(m.group(1)))
                    chan = str(rec.get("channel") or "").lower()
                    if not chan:
                        m = re.search(r"\((orderbook_delta|trade)\)",
                                      str(rec.get("detail") or ""))
                        chan = m.group(1) if m else ""
                    hit: Set[str] = set()
                    if chan == "trade":
                        pass                     # trade channel: books intact
                    else:
                        if named:
                            hit |= {t for t in named if t.startswith(prefix)}
                        for s in sids:
                            if s in sid_markets:
                                hit |= sid_markets[s]
                            elif chan == "orderbook_delta":
                                if s not in book_sids:
                                    hit |= all_ours()   # unseen book sid
                            elif s in book_sids or s in trade_sids:
                                pass             # not one of our books
                            else:
                                hit |= all_ours()       # unknown sid: be safe
                        if not named and not sids:
                            hit = all_ours()
                    if hit:
                        untrust(hit, rx)
                        if in_win(rx):
                            cnt["gaps"] += 1
                elif marker == "resubscribe":
                    named = [t for t in _marker_list(
                        rec, "markets", "market_tickers", "tickers",
                        "market_ticker", "ticker") if isinstance(t, str)]
                    hit = {t for t in named if t.startswith(prefix)}
                    if hit:
                        untrust(hit, rx)
                        if in_win(rx):
                            cnt["resubscribes"] += 1
                last_rx = rx
                continue

            last_rx = rx
            if conn == "unknown":
                conn = "up"                       # connected before the range
            m = rec.get("m")
            if not isinstance(m, dict):
                continue
            typ = m.get("type")
            body = m.get("msg") if isinstance(m.get("msg"), dict) else {}
            sid = m.get("sid")
            try:
                sid = int(sid) if sid is not None else None
            except (TypeError, ValueError):
                sid = None
            if typ in ("orderbook_snapshot", "orderbook_delta"):
                if sid is not None:
                    book_sids.add(sid)
            elif typ == "trade" and sid is not None:
                trade_sids.add(sid)
            # diagnostic per-sid seq check on OUR book sids (ok replies count,
            # as in kalshi_ws); never untrusts by itself
            seq = m.get("seq")
            if sid is not None and seq is not None:
                try:
                    seq = int(seq)
                    last = sid_seq.get(sid)
                    sid_seq[sid] = seq
                    if (last is not None and seq != last + 1 and seq != 1
                            and sid in sid_markets and in_win(rx)):
                        cnt["seq_anomalies"] += 1
                except (TypeError, ValueError):
                    pass
            tk = body.get("market_ticker") or body.get("ticker")
            if not tk or not isinstance(tk, str) or not tk.startswith(prefix):
                continue
            if typ == "orderbook_snapshot":
                y, n = snapshot_levels(body)
                ops.setdefault(tk, []).append((rx, _SNAP, y, n))
                seen.add(tk)
                if sid is not None:
                    sid_markets.setdefault(sid, set()).add(tk)
                cnt["snapshots"] += 1
                cnt["ws_rows"] += 1
            elif typ == "orderbook_delta":
                f = delta_fields(body)
                if f is not None:
                    ops.setdefault(tk, []).append((rx, _DELTA) + f)
                seen.add(tk)
                if sid is not None:
                    sid_markets.setdefault(sid, set()).add(tk)
                cnt["deltas"] += 1
                cnt["ws_rows"] += 1
            elif typ == "trade":
                p = parse_ws_trade(body, rx)
                if p is not None:
                    ws_trades.append(p)
                    seen.add(tk)
                cnt["ws_trades_raw"] += 1
                cnt["ws_rows"] += 1

        if conn == "down" and down_start is not None:
            down.append((down_start, hi))
        blackout: List[Tuple[float, float]] = []
        if status:
            prev = None
            for r in status_rx:
                if prev is not None and r - prev > STATUS_GAP_S:
                    blackout.append((prev, r + LIFE_MARGIN_S))
                prev = r
            if prev < self.t1 - STATUS_GAP_S:
                blackout.append((prev, hi))       # dead through the end
            blackout = _merge_spans(blackout)
            down.extend(blackout)
            cnt["recorder_gaps"] = sum(
                1 for a, b in blackout if a < self.t1 and b > self.t0)
        if last_rx is None:
            down.append((lo, hi))                 # nothing recorded at all
        elif not status and last_rx < self.t1 - TAIL_SILENCE_S:
            down.append((last_rx, hi))            # recorder silent at the end
        self._ops = ops
        self._down = down
        self._blackout = blackout

        # ---- settled file (REST trades, market objects) -----------------
        st = self._read_settled(settled_path)
        self.settled = st
        rest_rows: List[Tuple[Trade, Optional[str]]] = []
        if st:
            for m in st.get("markets") or []:
                t = m.get("ticker") if isinstance(m, dict) else None
                if t and t.startswith(prefix):
                    self.markets.setdefault(t, m)
            tr = st.get("trades")
            if tr is None and all(isinstance(k, str) and k.startswith(prefix)
                                  and isinstance(v, list)
                                  for k, v in st.items()):
                tr = st                         # bare {ticker: [trades]}
            for t, lst in (tr or {}).items():
                if not isinstance(lst, list):
                    continue
                for r in lst:
                    if not isinstance(r, dict):
                        continue
                    p = parse_rest_trade(r, t)
                    if p is not None and p[0].ticker.startswith(prefix):
                        rest_rows.append(p)

        self._merge_trades(ws_trades, rest_rows)

        # ---- our live orders (module doc 5) ------------------------------
        live_cov = None
        if self.subtract_live:
            lo = load_live_orders(ev, self.t0, self.t1, self.live_root)
            if lo:
                self.live_own = lo
                self.trades_all = list(self.trades)
                keep_t, keep_i = [], []
                for tr, info in zip(self.trades, self.trade_info):
                    if info[1] and str(info[1]) in lo.trade_ids:
                        self.own_trades.append(tr)
                    else:
                        keep_t.append(tr)
                        keep_i.append(info)
                self.trades, self.trade_info = keep_t, keep_i
                live_cov = {"orders": len(lo.orders), "fills": len(lo.fills),
                            "trades_removed": len(self.own_trades),
                            "contracts_removed": round(sum(t.count for t in self.own_trades), 2)}

        # ---- tickers -----------------------------------------------------
        tick = set(self.markets) | seen | set(ops)
        tick |= {t.ticker for t in self.trades}
        self.tickers = sorted(tick, key=_strike_key)

        # ---- coverage ----------------------------------------------------
        s0, s1 = math.floor(self.t0), math.ceil(self.t1)
        tf = {t: round(self.trusted_frac(t, s0, s1), 6) for t in self.tickers}
        n_ws = sum(1 for src, _ in self.trade_info if src == "ws")
        n_rest = len(self.trade_info) - n_ws
        src = ("mixed" if n_ws and n_rest else "ws" if n_ws
               else "rest" if n_rest else "none")
        down_secs = 0.0
        for a, b in _merge_spans(down):
            down_secs += max(0.0, min(b, self.t1) - max(a, self.t0))
        self.coverage = {
            "trusted_frac": tf,
            "book_trusted_frac": (round(sum(tf.values()) / len(tf), 6)
                                  if tf else 0.0),
            "gaps": cnt["gaps"], "reconnects": cnt["reconnects"],
            "disconnects": cnt["disconnects"],
            "silent_deaths": cnt["silent_deaths"],
            "resubscribes": cnt["resubscribes"],
            "seq_anomalies": cnt["seq_anomalies"],
            "ws_down_secs": round(down_secs, 3),
            "recorder_gaps": cnt.get("recorder_gaps", 0),
            "heartbeats": len(status),
            "ws_rows": cnt["ws_rows"], "snapshots": cnt["snapshots"],
            "deltas": cnt["deltas"], "dup_rows": cnt["dup_rows"],
            "trades": len(self.trades), "trades_ws": n_ws,
            "trades_rest": n_rest, "trades_src": src,
            "settled_file": bool(st),
        }
        if live_cov is not None:
            self.coverage["live_own"] = live_cov

    def _read_settled(self, settled_path: Optional[str]) -> Optional[dict]:
        paths = [settled_path] if settled_path else [
            os.path.join(C.DATA_ROOT, "settled", f"{self.event}.json"),
            os.path.join(C.DATA_ROOT, "backfill", "settled",
                         f"{self.event}.json")]
        for p in paths:
            if p and os.path.exists(p):
                d = C.read_json(p)
                if isinstance(d, dict):
                    return d
        return None

    def _merge_trades(self, ws: List[Tuple[Trade, Optional[str]]],
                      rest: List[Tuple[Trade, Optional[str]]]) -> None:
        """ws trades + every REST trade the ws did not deliver."""
        rest_by_id = {tid: tr for tr, tid in rest if tid}
        out: List[Tuple[Trade, str, Optional[str]]] = []
        ws_ids: Set[str] = set()
        no_id: Dict[tuple, List[float]] = {}
        seen_ws_ids: Set[str] = set()
        for tr, tid in ws:
            if tid:
                if tid in seen_ws_ids:
                    continue                    # duplicate delivery
                seen_ws_ids.add(tid)
                ws_ids.add(tid)
                r = rest_by_id.get(tid)
                if r is not None:
                    tr = tr._replace(ts=r.ts)   # exchange time to the us
            else:
                key = (tr.ticker, tr.taker_side, tr.yes_cents,
                       round(tr.count, 2))
                no_id.setdefault(key, []).append(tr.ts)
            out.append((tr, "ws", tid))
        for v in no_id.values():
            v.sort()
        seen_rest: Set[str] = set()
        for tr, tid in rest:
            if tid:
                if tid in ws_ids or tid in seen_rest:
                    continue
                seen_rest.add(tid)
            key = (tr.ticker, tr.taker_side, tr.yes_cents, round(tr.count, 2))
            lst = no_id.get(key)
            if lst:
                k = bisect.bisect_left(lst, tr.ts - FUZZY_TRADE_S)
                if k < len(lst) and lst[k] <= tr.ts + FUZZY_TRADE_S:
                    lst.pop(k)                  # matched an id-less ws trade
                    continue
            out.append((tr, "rest", tid))
        out = [o for o in out if self.t0 <= o[0].ts < self.t1]
        out.sort(key=lambda o: (o[0].ts, o[0].rx))
        self.trades = [o[0] for o in out]
        self.trade_info = [(o[1], o[2]) for o in out]

    def ws_down_spans(self) -> List[Tuple[float, float]]:
        """Merged spans (clipped to [t0, t1)) where the ws was down/unknown."""
        out = []
        for a, b in _merge_spans(self._down):
            a, b = max(a, self.t0), min(b, self.t1)
            if b > a:
                out.append((a, b))
        return out


def _merge_spans(spans: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _subtract_series(states: list, own: list, ticker: str) -> list:
    """One ticker's books_per_second states minus our per-second own orders
    (live_own_orders entries). Seconds where neither changed share one
    result tuple (the copy-on-write contract); untouched seconds keep the
    recorded objects."""
    out = list(states)
    last_key, last_val = None, None
    for i, b in enumerate(states):
        if b is None:
            continue
        o = own[i].get(ticker) if i < len(own) else None
        if not o:
            continue
        key = (id(b), id(o))        # both stay referenced by the inputs
        if key != last_key:
            last_key = key
            last_val = (subtract_levels(b[0], o[0]), subtract_levels(b[1], o[1]))
        out[i] = last_val
    return out


def load_event_stream(event_ticker: str, t0: float, t1: float,
                      lookback: float = LOOKBACK_S,
                      settled_path: Optional[str] = None,
                      subtract_live: Optional[bool] = None,
                      live_root: Optional[str] = None) -> EventStream:
    """Rebuild one event's books and trades over [t0, t1) from the recording
    (ws rows from t0 - lookback so each market's snapshot is found) plus the
    REST trades in settled/<EVENT>.json (backfill/settled/ as a fallback).

    subtract_live (None -> SUBTRACT_LIVE, default True): take our live
    executor's own resting orders out of the books and the trades that filled
    them out of the tape (module doc 5); a no-op when it logged none for the
    event. live_root overrides TEMP_MM_LIVE_DIR."""
    es = EventStream(event_ticker, t0, t1)
    es.subtract_live = SUBTRACT_LIVE if subtract_live is None else bool(subtract_live)
    es.live_root = live_root
    es._load(lookback, settled_path)
    return es


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) >= 2:
        evt = sys.argv[1]
        p = C.parse_event(evt)
        if not p:
            sys.exit(f"bad event ticker {evt}")
        settle = p[1]
        es = load_event_stream(evt, settle - 3600, settle)
        print(json.dumps({"tickers": es.tickers, "coverage": es.coverage},
                         indent=1))
    else:
        print("usage: python temp_shadow_scoring.py <EVENT>  (coverage summary)")
