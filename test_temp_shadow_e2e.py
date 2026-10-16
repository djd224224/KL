#!/usr/bin/env python3
"""End-to-end test of the hourly-temp shadow pipeline (TEMP_SHADOW.md).

SANDBOXED: TEMP_SHADOW_DATA points at a fresh temp dir BEFORE any
temp_shadow module is imported (temp_shadow_common reads it at import), so
nothing here can touch the live recording or the live sim / report dirs.

A synthetic but realistic recording is generated in the collector's exact
on-disk shapes (temp_shadow_collector docstring: ws `subscribed` / `ok` /
`orderbook_snapshot` / `orderbook_delta` / `trade` messages with sids, seqs,
dollar-string prices and fixed-point sizes; connect / gap / disconnect
markers; meta markets / programs / poll / status rows; wx minute rows that go
pending (4 stations, KFLL lagging ~45 s) -> pending (5) -> final with the
official v; settled/<EVENT>.json with REST trades), then the REAL scoring,
model, sim and report modules run on it:

  * 10/21 (shadow day 7): two full 10-strike events, 14:00 and 15:00 ET.
    The books are centred on the model's own fair at the open and re-centred
    after every station landing that moves the index; informed takers hit the
    stale touch around each landing; noise trades hit the touch every few
    minutes; event 2 carries one ws gap + re-snapshot. run_day sims both
    (worker pool), then temp_shadow_report.main() renders the day (no email).
  * 10/22: a one-strike event with a static book and a hand-written trade
    tape, replayed with a fixed custom policy so reward, fills, settlement
    P&L, mark-outs and fills_after_wx have exact expected values.

Run: PYTHONIOENCODING=utf-8 python -m unittest test_temp_shadow_e2e -v
"""
import os
import tempfile

# run_day's worker processes inherit the env: when this file is run as a
# script they re-import it as __mp_main__ and must reuse the parent's sandbox
_SANDBOX = os.environ.get("TSS_E2E_SANDBOX")
_OWNER = not _SANDBOX
if _OWNER:
    _SANDBOX = tempfile.mkdtemp(prefix="tss_e2e_")
    os.environ["TSS_E2E_SANDBOX"] = _SANDBOX
os.environ["TEMP_SHADOW_DATA"] = _SANDBOX

import atexit      # noqa: E402
import json        # noqa: E402
import math        # noqa: E402
import random      # noqa: E402
import shutil      # noqa: E402
import unittest    # noqa: E402
from contextlib import redirect_stdout  # noqa: E402
from io import StringIO   # noqa: E402
from unittest import mock  # noqa: E402

import temp_mm_policy as pol         # noqa: E402
import temp_shadow_common as C      # noqa: E402
import temp_shadow_model as tmodel  # noqa: E402
import temp_shadow_scoring as scoring  # noqa: E402
import temp_shadow_sim as sim       # noqa: E402
import temp_shadow_report as report  # noqa: E402

assert C.DATA_ROOT == os.environ["TEMP_SHADOW_DATA"]
assert os.path.realpath(C.DATA_ROOT).startswith(os.path.realpath(tempfile.gettempdir()))
if _OWNER:
    atexit.register(shutil.rmtree, _SANDBOX, True)

STATIONS = C.STATIONS["miami"]          # KMIA1M KOPF1M KFLL1M KFXE1M KPMP1M
KFLL = STATIONS.index("KFLL1M")
DAY = "2026-10-21"                       # shadow day 7 (DAY1 = 2026-10-15)
E1, E2 = "KXTEMPMIAH-26OCT2114", "KXTEMPMIAH-26OCT2115"
EX = "KXTEMPMIAH-26OCT2213"              # the exact-check event (10/22)
PID = 4242

# station landing lags (s after the minute): four stations at +143, KFLL at
# +188, the official v at +310; our poll sees each ~1.2 s later
RECV_LAG, KFLL_LAG = 143.0, 188.0
RX4, RX5, RXF = 144.2, 189.4, 311.3


def _dollars(c):
    return f"{c / 100.0:.4f}"


def _iso_ms(ts):
    whole, ms = divmod(int(round(ts * 1000)), 1000)
    return C.iso(whole)[:-1] + f".{ms:03d}Z"


def _iso_us(ts):
    whole, us = divmod(int(round(ts * 1e6)), 1000000)
    return C.iso(whole)[:-1] + f".{us:06d}Z"


def _uuid(rng):
    h = "%032x" % rng.getrandbits(128)
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def index_of(cs):
    return C.index_from_sum(sum(cs))


# --------------------------------------------------------------------------
# Synthetic recording
# --------------------------------------------------------------------------

class Recording:
    """Collects rows of all three raw streams; seqs and book snapshots are
    resolved at flush in rx order (sid 1 = orderbook_delta, sid 2 = trade,
    each with its own seq, `ok` replies take one too)."""

    def __init__(self, seed=7):
        self.rng = random.Random(seed)
        self.ws = []        # (rx, order, kind, payload)
        self.wx = []
        self.meta = []
        self.book_ops = {}  # ticker -> [(rx, side, cents, delta)]
        self.books0 = {}    # ticker -> (yes, no) at the first snapshot
        self.market_ids = {}
        self.trades = {}    # ticker -> [REST trade objects]
        self.n = 0

    # -- ws ------------------------------------------------------------
    def _push(self, rx, kind, payload):
        self.n += 1
        self.ws.append((rx, self.n, kind, payload))

    def marker(self, rx, kind, detail, **extra):
        self._push(rx, "marker", dict({"rx": rx, "marker": kind, "detail": detail}, **extra))

    def subscribed(self, rx):
        self._push(rx, "raw", {"type": "subscribed", "id": 1,
                               "msg": {"channel": "orderbook_delta", "sid": 1}})
        self._push(rx, "raw", {"type": "subscribed", "id": 2,
                               "msg": {"channel": "trade", "sid": 2}})

    def ok(self, rx, cid):
        self._push(rx, "seq1", {"type": "ok", "id": cid, "sid": 1,
                                "msg": {"market_tickers": []}})
        self._push(rx, "seq2", {"type": "ok", "id": cid + 1, "sid": 2,
                                "msg": {"market_tickers": []}})

    def snapshot(self, rx, ticker, book=None, skip_seq=False):
        """book given = the market's first snapshot; None = a re-snapshot of
        whatever the book is at rx (resolved at flush)."""
        if book is not None:
            self.books0[ticker] = (dict(book[0]), dict(book[1]))
            self.book_ops.setdefault(ticker, [])
            self.market_ids[ticker] = _uuid(self.rng)
        self._push(rx, "snap", {"ticker": ticker, "skip": skip_seq})

    def delta(self, rx, ticker, side, cents, d):
        if abs(d) < 1e-9:
            return
        self.book_ops.setdefault(ticker, []).append((rx, side, int(cents), float(d)))
        self._push(rx, "delta", {"ticker": ticker, "side": side, "cents": int(cents), "d": d})

    def trade(self, ts, ticker, taker, yes_c, count, block=False):
        """A trade at exchange time ts (we receive it 60 ms later), plus its
        REST copy for the settled file."""
        tid = _uuid(self.rng)
        body = {"trade_id": tid, "market_ticker": ticker,
                "yes_price_dollars": _dollars(yes_c), "no_price_dollars": _dollars(100 - yes_c),
                "count_fp": f"{count:.2f}", "taker_side": taker, "taker_outcome_side": taker,
                "taker_book_side": "bid" if taker == "yes" else "ask",
                "is_block_trade": bool(block), "ts": int(math.floor(ts)),
                "ts_ms": int(round(ts * 1000))}
        self._push(ts + 0.06, "trade", body)
        rest = {k: v for k, v in body.items() if k not in ("ts", "ts_ms", "market_ticker")}
        rest.update(ticker=ticker, created_time=_iso_us(ts))
        self.trades.setdefault(ticker, []).append(rest)

    def fill(self, ts, ticker, taker, side, lvl, yes_c, count):
        """A maker fill at exchange time ts: the book delta arrives just
        BEFORE the trade message (same ts_ms; temp_shadow_collector's verified
        order), both received after ts."""
        self.delta(ts + 0.055, ticker, side, lvl, -count)
        self.trade(ts, ticker, taker, yes_c, count)

    def book_at(self, ticker, rx):
        y, n = (dict(x) for x in self.books0[ticker])
        for orx, side, c, d in sorted(self.book_ops.get(ticker, []), key=lambda o: o[0]):
            if orx > rx:
                break
            b = y if side == "yes" else n
            q = round(b.get(c, 0.0) + d, 2)
            if q > 1e-9:
                b[c] = q
            else:
                b.pop(c, None)
        return y, n

    # -- meta / wx -----------------------------------------------------
    def meta_row(self, rx, kind, data):
        self.meta.append({"rx": rx, "kind": kind, "data": data})

    def heartbeats(self, t0, t1, started):
        t = t0 + 5.0
        while t < t1:
            self.meta_row(t, "status", {
                "pid": PID, "state": "running", "started": C.iso(started), "rx": t,
                "connected": True, "ws": {"connected": True, "last_msg_age_s": 0.4}})
            t += 60.0

    def polls(self, t0, t1):
        m = math.floor(t0 / 60.0) * 60.0
        while m + 60.0 < t1:
            self.meta_row(m + 60.4, "poll", {
                "t": int(m), "minute": C.iso(m), "n": 41, "n429": 0, "nerr": 0,
                "lat_ms_p50": 54.0, "lat_ms_max": 131.0, "signed": False, "n_signed": 0,
                "city": "miami"})
            m += 60.0

    def wx_minutes(self, t0, t1, temps, rec_start, rec_end):
        """Station rows for minutes [t0, t1): pending (4, no KFLL) -> pending
        (5) -> final with v. Rows the recorder could not have seen yet
        (rx < rec_start) arrive with its first poll: each minute's latest
        state at rec_start + 0.5 (last_sec=420 covers the last 7 minutes)."""
        early = {}
        m = t0
        while m < t1:
            cs = temps(m)

            def st(i, code):
                lag = KFLL_LAG if i == KFLL else RECV_LAG
                return {"station_id": STATIONS[i], "temp_f": round(cs[i] * 1.8 + 32.0, 1),
                        "code": code, "source": "synoptic",
                        "received_at_ms": int((m + lag) * 1000)}
            base = {"city": "miami", "t": int(m * 1000)}
            rows = [
                (m + RX4, dict(base, status="incomplete", v=None, contributors=None,
                               stations=[st(i, "pending") for i in range(5) if i != KFLL])),
                (m + RX5, dict(base, status="incomplete", v=None, contributors=None,
                               stations=[st(i, "pending") for i in range(5)])),
                (m + RXF, dict(base, status="normal", v=index_of(cs), contributors=5,
                               stations=[st(i, "ok") for i in range(5)])),
            ]
            for rx, r in rows:
                if rx < rec_start:
                    if rx >= rec_start - 420.0:
                        early[m] = r
                elif rx < rec_end:
                    self.wx.append(dict(r, rx=rx))
            m += 60.0
        for r in early.values():
            self.wx.append(dict(r, rx=rec_start + 0.5))

    # -- flush -----------------------------------------------------------
    def flush(self):
        seq = {1: 0, 2: 0}
        out = []
        for rx, _, kind, p in sorted(self.ws, key=lambda r: (r[0], r[1])):
            sent = int(rx * 1000) - 3
            if kind == "marker":
                rec = p
            elif kind == "raw":
                rec = {"rx": rx, "m": dict(p, sending_ts_ms=sent)}
            elif kind in ("seq1", "seq2"):
                sid = 1 if kind == "seq1" else 2
                seq[sid] += 1
                rec = {"rx": rx, "m": dict(p, seq=seq[sid], sending_ts_ms=sent)}
            elif kind == "snap":
                seq[1] += 2 if p["skip"] else 1
                y, n = self.book_at(p["ticker"], rx)
                msg = {"market_ticker": p["ticker"], "market_id": self.market_ids[p["ticker"]]}
                if y:
                    msg["yes_dollars_fp"] = [[_dollars(c), f"{q:.2f}"] for c, q in sorted(y.items())]
                if n:
                    msg["no_dollars_fp"] = [[_dollars(c), f"{q:.2f}"] for c, q in sorted(n.items())]
                rec = {"rx": rx, "m": {"type": "orderbook_snapshot", "sid": 1, "seq": seq[1],
                                       "msg": msg, "sending_ts_ms": sent}}
            elif kind == "delta":
                seq[1] += 1
                ts = rx - 0.004
                rec = {"rx": rx, "m": {"type": "orderbook_delta", "sid": 1, "seq": seq[1], "msg": {
                    "market_ticker": p["ticker"], "market_id": self.market_ids[p["ticker"]],
                    "price_dollars": _dollars(p["cents"]), "delta_fp": f"{p['d']:.2f}",
                    "side": p["side"], "ts": _iso_ms(ts), "ts_ms": int(ts * 1000)},
                    "sending_ts_ms": sent}}
            else:                                   # trade
                seq[2] += 1
                rec = {"rx": rx, "m": {"type": "trade", "sid": 2, "seq": seq[2], "msg": p,
                                       "sending_ts_ms": sent}}
            out.append(rec)
        for rec in out:
            C.append_jsonl(C.raw_file("ws", rec["rx"]), rec)
        for rec in sorted(self.wx, key=lambda r: r["rx"]):
            C.append_jsonl(C.raw_file("wx", rec["rx"]), rec)
        for rec in sorted(self.meta, key=lambda r: r["rx"]):
            C.append_jsonl(C.raw_file("meta", rec["rx"]), rec)


def market_objs(event, strikes, status, result=None, value=None):
    S = C.parse_event(event)[1]
    out = []
    for k in strikes:
        t = f"{event}-T{k:.2f}"
        m = {"ticker": t, "event_ticker": event, "floor_strike": k, "strike_type": "greater",
             "open_time": C.iso(S - 3600), "close_time": C.iso(S),
             "expected_expiration_time": C.iso(S + 300), "status": status,
             "result": "" if result is None else result[t],
             "expiration_value": "" if value is None else f"{value:.2f}",
             "market_type": "binary", "price_level_structure": "linear_cent"}
        out.append(m)
    return out


def programs_for(event, strikes, start_off=76.516631):
    S = C.parse_event(event)[1]
    return [{"discount_factor_bps": 5000, "end_date": C.iso(S),
             "id": f"prog-{event}-{i}", "incentive_description": "series_lip",
             "incentive_type": "liquidity", "market_ticker": f"{event}-T{k:.2f}",
             "paid_out": False, "period_reward": 1000000,
             "start_date": _iso_us(S - 3600 + start_off), "target_size_fp": "1000.00"}
            for i, k in enumerate(strikes)]


def write_settled(rec, event, strikes, value, progs):
    S = C.parse_event(event)[1]
    res = {f"{event}-T{k:.2f}": ("yes" if C.yes_wins(k, value) else "no") for k in strikes}
    C.write_json_atomic(C.settled_file(event), {
        "event_ticker": event, "fetched_rx": S + 430.0, "complete": True,
        "markets": market_objs(event, strikes, "finalized", res, value),
        "trades": {f"{event}-T{k:.2f}": rec.trades.get(f"{event}-T{k:.2f}", []) for k in strikes},
        "index": [{"t": int(S * 1000), "source": "kalshi_index",
                   "v": {"status": "normal", "temp_f": value}}],
        "programs": progs})
    return res


def centred_book(f):
    """A book around fair f: YES best 2c under the fair, NO best 2c under its
    fair, 300 / 500 / 600 lots; p >= 99 = locked at 99 (no NO side)."""
    p = int(round(100 * f))
    if p >= 99:
        return {99: 2000.0}, {}
    if p <= 1:
        return {1: 2000.0}, {98: 1200.0}
    y, n = {}, {}
    for b, d in ((y, max(1, p - 2)), (n, max(1, 98 - p))):
        for off, q in ((0, 300.0), (1, 500.0), (2, 600.0)):
            c = max(1, d - off)
            b[c] = b.get(c, 0.0) + q
    return y, n


# ---- day 10/21: two full events --------------------------------------------

S1 = C.parse_event(E1)[1]           # 18:00Z
S2 = C.parse_event(E2)[1]           # 19:00Z
OPEN1, OPEN2 = S1 - 3600, S2 - 3600
STRIKES1 = [77.99 + i for i in range(10)]
STRIKES2 = [78.99 + i for i in range(10)]
BASE_C = [28, 28, 29, 28, 28]       # 141 -> 82.76 F


def day_temps(m):
    """Whole-C readings for minute m (10/21): flat 82.76 F, a 5-minute KMIA
    bump to 83.12 in event 1, then a warming through event 2 to 84.20 F."""
    cs = list(BASE_C)
    if OPEN1 + 1320 <= m < OPEN1 + 1620:
        cs[0] = 29
    if m >= OPEN2 + 600:
        cs[1] = 29                  # 142 -> 83.12
    if m >= OPEN2 + 1800:
        cs[3] = 29                  # 143 -> 83.48
    if m >= OPEN2 + 2700:
        cs[4] = 29                  # 144 -> 83.84
    if m >= OPEN2 + 3120:
        cs[0] = 29                  # 145 -> 84.20 (settles E2)
    return cs


GAP_AT = OPEN2 + 1230.0             # E2's ws gap marker (re-snapshots +2.5 s)
SETTLE1 = index_of(day_temps(S1))   # 82.76
SETTLE2 = index_of(day_temps(S2))   # 84.20


def model_fairs(rows, settle, strikes, at):
    tape = tmodel.WxTape(rows)
    fm = tmodel.FairModel()
    return fm.fair(tape.snapshot(at), settle, strikes)


def build_day(rec):
    rng = random.Random(11)
    rec_start, rec_end = OPEN1 - 92 * 60, S2 + 12 * 60
    rec.marker(rec_start + 1.0, "connect", C.WS_URL)
    rec.heartbeats(rec_start, rec_end, rec_start)
    rec.polls(rec_start, rec_end)
    rec.wx_minutes(math.floor(rec_start / 60) * 60 - 600, S2 + 60, day_temps, rec_start, rec_end)
    wx_rows = list(rec.wx)
    cid = 3
    settle_specs = []
    for ev, strikes, op, S, value in ((E1, STRIKES1, OPEN1, S1, SETTLE1),
                                      (E2, STRIKES2, OPEN2, S2, SETTLE2)):
        tickers = [f"{ev}-T{k:.2f}" for k in strikes]
        disc = op - 600.0 + 3.1
        rec.meta_row(disc, "markets", {"event_ticker": ev,
                                       "markets": market_objs(ev, strikes, "initialized")})
        rec.meta_row(op + 4.2, "markets", {"event_ticker": ev,
                                           "markets": market_objs(ev, strikes, "active")})
        rec.meta_row(S + 29.0, "markets", {"event_ticker": ev,
                                           "markets": market_objs(ev, strikes, "closed")})
        progs = programs_for(ev, strikes)
        rec.meta_row(op + 3.0, "programs", {"programs": []})
        rec.meta_row(op + 182.7, "programs", {"programs": progs})
        # books centred on the model's fair at discovery
        fairs = model_fairs(wx_rows, S, strikes, disc)
        if ev == E1:
            rec.subscribed(disc + 0.3)
        else:
            rec.ok(disc + 0.3, cid)
            cid += 2
        books = {}
        for t, k in zip(tickers, strikes):
            books[t] = centred_book(fairs[k])
            rec.snapshot(disc + 0.35, t, books[t])
        # every est-changing landing: informed takers hit the stale touch
        # (0.2 s before and 0.3 s after our poll sees it), then the book
        # re-centres on the new fair 2 s later
        tape = tmodel.WxTape(wx_rows)
        fm = tmodel.FairModel()
        prev_est = None
        for r in sorted(wx_rows, key=lambda r: r["rx"]):
            rx = r["rx"]
            if rx < disc or rx >= S:
                continue
            sn = tape.snapshot(rx)
            est = sn.est_latest
            if prev_est is None or est is None or abs(est - prev_est) < 1e-6:
                prev_est = est if est is not None else prev_est
                continue
            prev_est = est
            new_f = fm.fair(sn, S, strikes)
            for t, k in zip(tickers, strikes):
                y, n = rec.book_at(t, rx - 0.5)
                nb = centred_book(new_f[k])
                by, bn = max(y) if y else None, max(n) if n else None
                old_p = (by + 100 - bn) / 2.0 if by and bn else None
                if old_p is not None and abs(100 * new_f[k] - old_p) >= 3:
                    up = 100 * new_f[k] > old_p
                    side, lvl = ("no", bn) if up else ("yes", by)
                    q = (n if up else y)[lvl]
                    yc = 100 - lvl if up else lvl
                    taker = "yes" if up else "no"
                    for dt, share in ((-0.2, 0.4), (0.3, 0.6)):
                        cnt = math.floor(q * share)
                        rec.fill(rx + dt, t, taker, side, lvl, yc, cnt)
                # re-centre: diff the current book against the new one
                cy, cn = rec.book_at(t, rx + 1.99)
                for side, cur, new in (("yes", cy, nb[0]), ("no", cn, nb[1])):
                    for c in sorted(set(cur) | set(new)):
                        rec.delta(rx + 2.0, t, side, c, new.get(c, 0.0) - cur.get(c, 0.0))
        # noise: every ~3 min a small trade at the touch of a live strike
        # (second 50-57, outside the landing window), replenished 3 s later
        t_n = op + 200.0
        while t_n < S - 30:
            t_n += rng.uniform(120, 240)
            ts = math.floor(t_n / 60) * 60 + rng.uniform(50, 57)
            if ts >= S - 5:
                break
            live = [t for t in tickers if 8 <= (lambda b: (max(b[0]) + 100 - max(b[1])) / 2.0
                                               if b[0] and b[1] else 0)(rec.book_at(t, ts)) <= 92]
            if not live:
                continue
            t = rng.choice(live)
            y, n = rec.book_at(t, ts)
            taker = rng.choice(("yes", "no"))
            side, lvl = ("no", max(n)) if taker == "yes" else ("yes", max(y))
            cnt = float(rng.randint(5, 60))
            rec.fill(ts, t, taker, side, lvl, 100 - lvl if taker == "yes" else lvl, cnt)
            rec.delta(ts + 3.0, t, side, lvl, cnt)
        if ev == E2:
            # one ws gap on the book sid: untrusted until the re-snapshot
            g = GAP_AT
            rec.marker(g, "gap", "sid 1 (orderbook_delta) expected seq 4130 got 4131",
                       sid=1, channel="orderbook_delta", expected=4130, got=4131)
            for i, t in enumerate(tickers):
                rec.snapshot(g + 2.5 + 0.01 * i, t, skip_seq=(i == 0))
        # a block trade far through the book: must never fill anyone
        rec.trade(op + 1777.7, tickers[4], "no", 2, 5000.0, block=True)
        settle_specs.append((ev, strikes, value, progs))
    rec.marker(rec_end - 1.0, "disconnect", "shutdown")
    return settle_specs


# ---- 10/22: one strike, static book, exact expectations ---------------------

SX = C.parse_event(EX)[1]
OPENX = SX - 3600
KX = 82.99
TX = f"{EX}-T{KX:.2f}"
BOOK_XY = {30: 300.0, 29: 500.0, 28: 600.0}
BOOK_XN = {60: 300.0, 59: 500.0, 58: 600.0}
X_PROG_START = OPENX + 76.516631
X_BUMP = OPENX + 1800                # KMIA 28 -> 29 for 3 minutes
X_POLICY = {"X": dict(window_mode="none", fair_guard=False, live_tau_full=0.0,
                      live_stop_min=0.0, qual_pads=False)}
# trades (seconds after open): t1 lands 1.1 s after a KFLL landing (a
# composition flip of index_latest, not a temperature change); t3 lands 0.4 s
# after our poll sees the bump (a real index change) and before our reaction
X_T1 = OPENX + 720 + RX5 + 1.1       # taker no @30 x200: equal to our YES bid
X_T2 = OPENX + 1500.3                # taker no @29 x50: through our YES bid
X_T3 = X_BUMP + RX4 + 0.4            # taker yes @40 x500: equal to our NO bid
X_BLOCK = OPENX + 2500.0             # block trade @25 x999: ignored


def x_temps(m):
    cs = list(BASE_C)
    if X_BUMP <= m < X_BUMP + 180:
        cs[0] = 29
    return cs


SETTLEX = index_of(x_temps(SX))      # 82.76 -> T82.99 settles NO


def build_x(rec):
    rec_start, rec_end = OPENX - 92 * 60, SX + 12 * 60
    rec.marker(rec_start + 1.0, "connect", C.WS_URL)
    rec.heartbeats(rec_start, rec_end, rec_start)
    rec.polls(rec_start, rec_end)
    rec.wx_minutes(math.floor(rec_start / 60) * 60 - 600, SX + 60, x_temps, rec_start, rec_end)
    disc = OPENX - 600.0 + 2.0
    rec.meta_row(disc, "markets", {"event_ticker": EX, "markets": market_objs(EX, [KX], "initialized")})
    rec.meta_row(SX + 29.0, "markets", {"event_ticker": EX, "markets": market_objs(EX, [KX], "closed")})
    progs = programs_for(EX, [KX])
    rec.meta_row(OPENX + 190.0, "programs", {"programs": progs})
    rec.subscribed(disc + 0.3)
    rec.snapshot(disc + 0.35, TX, (BOOK_XY, BOOK_XN))
    rec.trade(X_T1, TX, "no", 30, 200.0)
    rec.trade(X_T2, TX, "no", 29, 50.0)
    rec.trade(X_T3, TX, "yes", 40, 500.0)
    rec.trade(X_BLOCK, TX, "no", 25, 999.0, block=True)
    rec.marker(rec_end - 1.0, "disconnect", "shutdown")
    return progs


def build_all():
    root = os.path.realpath(C.DATA_ROOT)
    assert root.startswith(os.path.realpath(tempfile.gettempdir()))
    for sub in ("raw", "settled", "sim", "reports", "model"):
        shutil.rmtree(os.path.join(root, sub), ignore_errors=True)
    rec = Recording(seed=7)
    specs = build_day(rec)
    progs_x = build_x(rec)
    rec.flush()
    results = {}
    for ev, strikes, value, progs in specs:
        results[ev] = write_settled(rec, ev, strikes, value, progs)
    results[EX] = write_settled(rec, EX, [KX], SETTLEX, progs_x)
    return rec, results


REC = None
DAY_RESULTS = None


def setUpModule():
    global REC, DAY_RESULTS
    REC, _ = build_all()
    DAY_RESULTS = sim.run_day(DAY, workers=2)


# --------------------------------------------------------------------------
# Ground truth from the generator (independent of scoring's replay)
# --------------------------------------------------------------------------


def truth_market_stats(rec, event, strikes, progs):
    """locked99 / paying fractions over program seconds, straight from the
    generator's book timeline (first snapshot + every delta), skipping the
    seconds the sim must treat as untrusted (the ws gap until re-snapshot)."""
    by_t = {p["market_ticker"]: p for p in progs}
    n = locked = paying = 0
    for i_k, k in enumerate(strikes):
        t = f"{event}-T{k:.2f}"
        st = C.parse_iso(by_t[t]["start_date"])
        en = C.parse_iso(by_t[t]["end_date"])
        y, nb = (dict(x) for x in rec.books0[t])
        ops = sorted(rec.book_ops.get(t, []), key=lambda o: o[0])
        j = 0
        sec0 = int(math.floor(en - 3600))
        for i in range(3600):
            T = sec0 + i + 0.5
            while j < len(ops) and ops[j][0] <= T:
                _, side, c, d = ops[j]
                b = y if side == "yes" else nb
                q = round(b.get(c, 0.0) + d, 2)
                if q > 1e-9:
                    b[c] = q
                else:
                    b.pop(c, None)
                j += 1
            if not (st <= T < en):
                continue
            if event == E2 and GAP_AT <= T < GAP_AT + 2.5 + 0.01 * i_k:
                continue
            n += 1
            locked += (bool(y) and max(y) >= 99) or (bool(nb) and max(nb) >= 99)
            paying += scoring.score(y, nb, {}, {})[1]
    return {"strike_secs": n, "locked99_frac": locked / n,
            "paying_frac_without_us": paying / n}


def ws_trades_in(rec, event, t0, t1):
    return [p for rx, _, kind, p in rec.ws
            if kind == "trade" and p["market_ticker"].startswith(event + "-")
            and not p["is_block_trade"] and t0 <= p["ts_ms"] / 1000.0 < t1]


SCHEMA_TOP = {"event", "open_ts", "close_ts", "settle_ts", "settled", "settlement_value",
              "results", "coverage", "market_stats", "model_eval", "variants"}
SCHEMA_COV = {"book_trusted_frac", "wx_rows", "trades", "trades_src", "reconnects", "gaps",
              "program_secs"}
SCHEMA_MS = {"strike_secs", "paying_frac_without_us", "locked99_frac", "live_frac",
             "decided_frac", "depth3c_median"}
SCHEMA_ME = {"n", "brier_model", "brier_mid", "logloss_model", "logloss_mid", "by_tau"}
SCHEMA_VAR = {"reward", "fills_n", "contracts", "trading_pnl", "fees", "net", "avg_share",
              "quoted_secs", "max_abs_pos", "worst_case_loss", "by_class", "by_tau",
              "by_side", "fills_after_wx", "markouts"}
TAUS = ("0-5", "5-10", "10-20", "20-40", "40-60")


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

class TestDaySim(unittest.TestCase):
    """run_day on the two 10/21 events (all VARIANTS x opt/cons, 2 workers)."""

    def res(self, ev):
        return next(r for r in DAY_RESULTS if r.get("event") == ev)

    def test_run_day_outputs(self):
        self.assertEqual([r.get("event") for r in DAY_RESULTS], [E1, E2])
        for r in DAY_RESULTS:
            self.assertNotIn("error", r, r.get("trace"))
            self.assertNotIn("pending", r)
            self.assertTrue(r["settled"])
            self.assertEqual(r["sim_version"], sim.SIM_VERSION)
            self.assertEqual(r["flags"], [])
            path = os.path.join(C.DATA_ROOT, "sim", DAY, f"{r['event']}.json")
            self.assertEqual(C.read_json(path)["variants"],
                             json.loads(json.dumps(r["variants"])))
            self.assertEqual(set(r["variants"]),
                             {f"{v}|{f}" for v in sim.VARIANTS for f in sim.FILL_MODELS})
        # a second call reuses the files (same variants, same SIM_VERSION)
        again = sim.run_day(DAY, workers=1)
        self.assertEqual([a["runtime_s"] for a in again], [r["runtime_s"] for r in DAY_RESULTS])

    def test_schema(self):
        for r in DAY_RESULTS:
            self.assertLessEqual(SCHEMA_TOP, set(r))
            self.assertLessEqual(SCHEMA_COV, set(r["coverage"]))
            self.assertLessEqual(SCHEMA_MS, set(r["market_stats"]))
            self.assertLessEqual(SCHEMA_ME, set(r["model_eval"]))
            self.assertEqual(set(r["model_eval"]["by_tau"]), set(TAUS))
            for k, v in r["variants"].items():
                self.assertLessEqual(SCHEMA_VAR, set(v), k)
                self.assertEqual(set(v["by_class"]), {"live", "decided"})
                self.assertEqual(set(v["by_tau"]), set(TAUS))
                self.assertEqual(set(v["by_side"]), {"yes_bid", "no_bid"})
                self.assertEqual(set(v["markouts"]), {"m60", "m300"})

    def test_settlement_and_results(self):
        for ev, value, strikes in ((E1, SETTLE1, STRIKES1), (E2, SETTLE2, STRIKES2)):
            r = self.res(ev)
            self.assertAlmostEqual(r["settlement_value"], value)
            for k in strikes:
                t = f"{ev}-T{k:.2f}"
                self.assertEqual(r["results"][t], "yes" if C.yes_wins(k, value) else "no")
            self.assertEqual(r["close_ts"], C.parse_event(ev)[1])
            self.assertEqual(r["open_ts"], C.parse_event(ev)[1] - 3600)

    def test_coverage(self):
        for ev in (E1, E2):
            cov = self.res(ev)["coverage"]
            S = C.parse_event(ev)[1]
            self.assertEqual(cov["trades_src"], "ws")
            self.assertEqual(cov["programs_src"], "meta")
            self.assertEqual(cov["reconnects"], 0)
            self.assertEqual(cov["wx_rows"], 180)            # 3 rows a minute
            self.assertGreater(cov["wx_rows_lookback"], 200)
            self.assertEqual(cov["fair_missing_frac"], 0.0)
            self.assertEqual(cov["program_secs"], 3523)
            # every non-block ws trade of the hour; the block trade dropped
            self.assertEqual(cov["trades"], len(ws_trades_in(REC, ev, S - 3600, S)))
        c1, c2 = self.res(E1)["coverage"], self.res(E2)["coverage"]
        self.assertEqual(c1["book_trusted_frac"], 1.0)
        self.assertEqual(c1["gaps"], 0)
        self.assertEqual(c2["gaps"], 1)
        # 10 books dark from the gap marker to their re-snapshot (~2.5 s each)
        self.assertAlmostEqual(c2["book_trusted_frac"], 1.0 - 26.0 / 36000.0,
                               delta=12.0 / 36000)

    def test_market_stats_match_generator_truth(self):
        for ev, strikes in ((E1, STRIKES1), (E2, STRIKES2)):
            ms = self.res(ev)["market_stats"]
            tr = truth_market_stats(REC, ev, strikes, programs_for(ev, strikes))
            self.assertAlmostEqual(ms["strike_secs"], tr["strike_secs"], delta=15)
            self.assertAlmostEqual(ms["locked99_frac"], tr["locked99_frac"], delta=0.002)
            self.assertAlmostEqual(ms["paying_frac_without_us"], tr["paying_frac_without_us"],
                                   delta=0.002)
            self.assertGreater(ms["locked99_frac"], 0.05)        # a locked strike exists
            self.assertAlmostEqual(ms["live_frac"] + ms["decided_frac"], 1.0, places=5)
            self.assertEqual(ms["depth3c_median"], 1400.0)       # 300+500+600 a side

    def test_model_eval(self):
        for ev in (E1, E2):
            me = self.res(ev)["model_eval"]
            self.assertGreater(me["n"], 300)
            self.assertEqual(sum(b["n"] for b in me["by_tau"].values()), me["n"])
            for k in ("brier_model", "brier_mid"):
                self.assertTrue(0.0 <= me[k] < 0.15, (ev, k, me[k]))
            for k in ("logloss_model", "logloss_mid"):
                self.assertTrue(0.0 < me[k] < 0.7, (ev, k, me[k]))

    def test_variant_identities(self):
        for r in DAY_RESULTS:
            for k, v in r["variants"].items():
                msg = f"{r['event']} {k}"
                self.assertAlmostEqual(v["net"], v["reward"] + v["trading_pnl"] - v["fees"],
                                       places=4, msg=msg)
                self.assertAlmostEqual(sum(c["reward"] for c in v["by_class"].values()),
                                       v["reward"], places=3, msg=msg)
                self.assertAlmostEqual(sum(b["reward"] for b in v["by_tau"].values()),
                                       v["reward"], places=3, msg=msg)
                for part in ("by_class", "by_tau", "by_side"):
                    self.assertAlmostEqual(sum(b["contracts"] for b in v[part].values()),
                                           v["contracts"], places=2, msg=f"{msg} {part}")
                    self.assertAlmostEqual(sum(b["pnl"] for b in v[part].values()),
                                           v["trading_pnl"], places=3, msg=f"{msg} {part}")
                # reward: at most the ten $100 pools; share a fraction
                self.assertTrue(0.0 <= v["reward"] <= 1000.0, msg)
                self.assertTrue(0.0 <= v["avg_share"] <= 1.0, msg)
                self.assertLessEqual(v["quoted_secs"], r["market_stats"]["strike_secs"], msg)
                if v["contracts"] > 0:
                    # swp: the mean over its Monte Carlo draws (a fill in 1 of 8 = 0.125)
                    if k.endswith("|swp"):
                        self.assertGreater(v["fills_n"], 0, msg)
                    else:
                        self.assertGreaterEqual(v["fills_n"], 1, msg)
                self.assertLessEqual(v["fills_after_wx"]["contracts"], v["contracts"] + 1e-6)
                self.assertLessEqual(v["max_abs_pos"], 1500.0 + 1e-6)
                self.assertLessEqual(v["worst_case_loss"], 1500.0 + 1e-6)

    def test_policy_structure(self):
        for r in DAY_RESULTS:
            V = r["variants"]
            for fm in sim.FILL_MODELS:
                A, B = V[f"A_live300_nowin|{fm}"], V[f"B_live300_pull|{fm}"]
                D, G = V[f"D_live600_pull|{fm}"], V[f"G_decided_only|{fm}"]
                # the landing-window pull quotes fewer seconds than no window
                self.assertLess(B["quoted_secs"], A["quoted_secs"])
                # twice the size at the same prices: a larger share and reward
                self.assertGreater(D["avg_share"], B["avg_share"])
                self.assertGreater(D["reward"], B["reward"])
                # decided-only never quotes a live strike; live-only never a
                # decided one
                self.assertEqual(G["by_class"]["live"]["reward"], 0.0)
                self.assertEqual(G["by_class"]["live"]["contracts"], 0.0)
                self.assertGreater(G["by_class"]["decided"]["reward"], 0.0)
                for name in ("A_live300_nowin", "B_live300_pull", "C_live300_back",
                             "D_live600_pull", "H_noguard_pull"):
                    self.assertEqual(V[f"{name}|{fm}"]["by_class"]["decided"]["reward"], 0.0)
                # every variant earns something on these two-sided books --
                # except outside its quote hours (FAV_night: settle 20-07 ET)
                for k, v in V.items():
                    P = sim._resolve_variants([k.split("|")[0]])[k.split("|")[0]]
                    if pol.quote_hour_ok(P, r["settle_ts"]):
                        self.assertGreater(v["reward"], 0.0, k)
                    else:
                        self.assertEqual((v["reward"], v["quoted_secs"], v["contracts"]),
                                         (0.0, 0, 0.0), k)
            tot = {fm: sum(v["contracts"] for k, v in V.items() if k.endswith("|" + fm))
                   for fm in sim.FILL_MODELS}
            self.assertGreaterEqual(tot["opt"], tot["cons"])

    def test_informed_flow_at_landings(self):
        """Event 2 warms through three station landings; takers sweep the
        stale touch 0.2 s before and 0.3 s after our poll sees each one. The
        no-window variant joins that touch and is picked off (losing,
        after-wx fills); the pull variants are out of the book then."""
        V = self.res(E2)["variants"]
        A = V["A_live300_nowin|opt"]
        self.assertGreater(A["fills_after_wx"]["contracts"], 0.0)
        self.assertLess(A["fills_after_wx"]["pnl"], 0.0)
        self.assertLess(A["trading_pnl"], 0.0)
        # cons: the 300 resting at the touch ahead of us take the whole sweep
        # (the fill deltas arrive after the trade's exchange time, so the
        # queue is not shrunk by the very trade that consumes it)
        self.assertLess(V["A_live300_nowin|cons"]["fills_after_wx"]["contracts"],
                        A["fills_after_wx"]["contracts"])
        for fm in sim.FILL_MODELS:
            for name in ("B_live300_pull", "D_live600_pull", "H_noguard_pull"):
                self.assertEqual(V[f"{name}|{fm}"]["fills_after_wx"]["contracts"], 0.0,
                                 f"{name}|{fm}")


class TestExactEvent(unittest.TestCase):
    """The 10/22 one-strike event under a fixed policy: no window, no guard,
    300 a side all hour (no tau taper, no pads) at the reference prices."""

    @classmethod
    def setUpClass(cls):
        cls.res = sim.run_event(EX, variants=X_POLICY)

    def test_inputs(self):
        r = self.res
        self.assertEqual(r["flags"], [])
        self.assertTrue(r["settled"])
        self.assertEqual(r["results"], {TX: "no"})
        self.assertAlmostEqual(r["settlement_value"], 82.76)
        self.assertEqual(r["coverage"]["trades"], 3)            # block trade dropped
        self.assertEqual(r["coverage"]["book_trusted_frac"], 1.0)
        self.assertEqual(r["market_stats"]["live_frac"], 1.0)

    def test_reward_exact(self):
        frac, counted, _, _ = scoring.score(BOOK_XY, BOOK_XN, {30: 300.0}, {60: 300.0})
        self.assertTrue(counted)
        pool = 100.0 / (SX - X_PROG_START)
        # orders go live 0.15 s after the first decision whose activation
        # window overlaps the program; ticks sit at :xx.5, so every tick from
        # the program start on is scored
        n_ticks = sum(1 for i in range(3600) if X_PROG_START <= OPENX + i + 0.5 < SX)
        for fm in sim.FILL_MODELS:
            v = self.res["variants"][f"X|{fm}"]
            self.assertEqual(v["quoted_secs"], n_ticks)
            # a fill leaves the order short for one tick until the re-quote
            self.assertAlmostEqual(v["reward"], pool * frac * n_ticks, delta=3 * pool, msg=fm)
            self.assertAlmostEqual(v["avg_share"], frac, delta=3.0 / n_ticks)
            self.assertAlmostEqual(v["by_class"]["live"]["reward"], v["reward"], places=6)

    def test_fills_and_pnl_exact(self):
        opt = self.res["variants"]["X|opt"]
        cons = self.res["variants"]["X|cons"]
        # t1 taker no @30 x200 (= our YES bid): opt fills 200, cons is behind
        # the 300 queue; t2 @29 x50 (through our bid): both fill 50; t3 taker
        # yes @40 x500 (= our NO bid 60): opt 300, cons 500 - 300 queue = 200.
        # T82.99 settles NO (82.76): YES bids at 30 lose 30c, NO bids at 60
        # win 40c.
        self.assertEqual((opt["fills_n"], cons["fills_n"]), (3, 2))
        self.assertEqual((opt["contracts"], cons["contracts"]), (550.0, 250.0))
        self.assertAlmostEqual(opt["trading_pnl"], 250 * -0.30 + 300 * 0.40, places=6)
        self.assertAlmostEqual(cons["trading_pnl"], 50 * -0.30 + 200 * 0.40, places=6)
        self.assertEqual(opt["by_side"], {"yes_bid": {"contracts": 250.0, "pnl": -75.0},
                                          "no_bid": {"contracts": 300.0, "pnl": 120.0}})
        self.assertEqual(cons["by_side"], {"yes_bid": {"contracts": 50.0, "pnl": -15.0},
                                           "no_bid": {"contracts": 200.0, "pnl": 80.0}})
        self.assertEqual(opt["by_tau"]["40-60"]["contracts"], 200.0)       # t1 (tau 44.8)
        self.assertEqual(opt["by_tau"]["20-40"]["contracts"], 350.0)       # t2, t3
        self.assertEqual(cons["by_tau"]["40-60"]["contracts"], 0.0)
        self.assertEqual((opt["max_abs_pos"], cons["max_abs_pos"]), (250.0, 150.0))
        self.assertEqual((opt["worst_case_loss"], cons["worst_case_loss"]), (75.0, 85.0))
        # the book mid is a static 35c: +5c a contract on both sides
        for v in (opt, cons):
            self.assertEqual(v["markouts"], {"m60": 5.0, "m300": 5.0})
            self.assertAlmostEqual(v["net"], v["reward"] + v["trading_pnl"], places=5)

    def test_fills_after_wx_uses_real_index_moves(self):
        """t3 lands 0.4 s after our poll sees KMIA step up (the five-station
        estimate moves): after-wx. t1 lands 1.1 s after KFLL's lagging
        reading completes its minute -- index_latest steps from the 4-station
        replica to the 5-station value there with no temperature change: NOT
        after-wx."""
        self.assertEqual(self.res["variants"]["X|opt"]["fills_after_wx"],
                         {"contracts": 300.0, "pnl": 120.0})
        self.assertEqual(self.res["variants"]["X|cons"]["fills_after_wx"],
                         {"contracts": 200.0, "pnl": 80.0})
        # the composition flip is really there in the tape
        rows = [r for r in REC.wx if OPENX - 4500 <= r["rx"] < SX]
        tape = tmodel.WxTape(rows)
        a = tape.snapshot(X_T1 - 1.5)
        a_idx, a_est = a.index_latest, a.est_latest
        b = tape.snapshot(X_T1 - 0.5)
        self.assertGreater(abs(a_idx - b.index_latest), 0.01)
        self.assertAlmostEqual(a_est, b.est_latest, places=6)

    def test_guard_slopes_from_model(self):
        """The guard buffer is FairModel.slopes (the mixture slope on the
        fair's own cut), not a plain normal on dist()'s core sigma."""
        rows = [r for r in REC.wx if OPENX - sim.WX_LOOKBACK_S <= r["rx"] < SX]
        fm = tmodel.FairModel()
        sh = sim._Shared(sim.load_event_inputs(EX), fm, None)
        self.assertGreater(len(sh.st_rx), 10)
        for j in (0, len(sh.st_rx) // 2, len(sh.st_rx) - 1):
            snap = tmodel.WxTape(rows).snapshot(sh.st_rx[j])
            self.assertAlmostEqual(sh.st_g[j][0], fm.slopes(snap, SX, [KX])[KX], places=9)
            self.assertAlmostEqual(sh.st_f[j][0], fm.fair(snap, SX, [KX])[KX], places=9)


class TestReport(unittest.TestCase):
    """temp_shadow_report.main() for 10/21 (day 7) on the sim output, as the
    08:30 ET task would run it the next morning -- without --send."""

    NOW = C.parse_iso("2026-10-22T12:30:00Z")

    @classmethod
    def setUpClass(cls):
        cls.out = os.path.join(C.DATA_ROOT, "out", "report.html")
        buf = StringIO()
        with mock.patch.object(C, "utcnow", return_value=cls.NOW), \
                mock.patch.object(report, "send_report",
                                  side_effect=AssertionError("must not email")), \
                redirect_stdout(buf):
            cls.rc = report.main(["--day", DAY, "--out", cls.out])
            cls.ctx = report.build_context(DAY, now=cls.NOW)
        cls.stdout = buf.getvalue()
        with open(cls.out, encoding="utf-8") as f:
            cls.page = f.read()

    def test_runs_clean(self):
        self.assertEqual(self.rc, 0, self.stdout)
        self.assertIn("sim: 2 ok, 0 pending, 0 failed", self.stdout)
        self.assertNotIn("Traceback", self.page)
        self.assertNotIn("THE SIM FAILED", self.page)

    def test_all_sections(self):
        for h in ("Headline", "Variants", "Breakdown of", "Market structure", "Model",
                  "Data health", "Suggested tweaks", "Cumulative to date", "Week summary"):
            self.assertIn(f">{h}", self.page, h)
        self.assertIn("Day 7 of 7", self.page)
        self.assertIn("Week verdict:", self.page)
        self.assertIn(self.ctx["best_cons"], self.page)
        for v in sim.VARIANTS:
            self.assertIn(v, self.page)
        self.assertIn("day 7", report.subject_line(self.ctx).lower())

    def test_aggregates_equal_sim_output(self):
        agg = self.ctx["agg"]
        self.assertEqual(len(self.ctx["events"]), 2)
        for key in DAY_RESULTS[0]["variants"]:
            for f in ("reward", "trading_pnl", "net", "contracts", "fills_n"):
                want = sum(r["variants"][key][f] for r in DAY_RESULTS)
                self.assertAlmostEqual(agg[key][f], want, places=4, msg=f"{key} {f}")
            self.assertEqual(agg[key]["n_events"], 2)
        best = self.ctx["best_cons"]
        self.assertEqual(best, max((k.split("|")[0] for k in agg if k.endswith("|cons")),
                                   key=lambda n: agg[f"{n}|cons"]["net"]))
        ms = self.ctx["market"]
        w = [r["market_stats"]["strike_secs"] for r in DAY_RESULTS]
        want = sum(r["market_stats"]["locked99_frac"] * x
                   for r, x in zip(DAY_RESULTS, w)) / sum(w)
        self.assertAlmostEqual(ms["locked99_frac"], want, places=6)
        self.assertEqual(self.ctx["model"]["n"], sum(r["model_eval"]["n"] for r in DAY_RESULTS))

    def test_health_reads_the_recording(self):
        h = self.ctx["health"]
        self.assertTrue(h["started_late"])                   # recording began 11:28 ET
        self.assertEqual(h["events"]["simmed"], 2)
        # 12:00 .. 23:00 ET settle after the recording began; 2 simmed, the
        # rest were never listed in this recording
        self.assertEqual(h["events"]["due"], 12)
        self.assertEqual(len(h["events"]["missing"]), 10)
        # the replica reproduces every official v the generator wrote
        self.assertGreater(h["replica"]["n"], 200)
        self.assertEqual(h["replica"]["rate"], 1.0)


class TestReportShadowWindow(unittest.TestCase):
    """An event that closed before the recording began (the collector's
    26 h recovery pull writes its settled file) is simmed dark by run_day but
    stays out of the report's figures -- the same "outside the shadow" rule
    the health section applies. (Runs after TestReport: it adds an event.)"""

    EV0 = "KXTEMPMIAH-26OCT2109"         # 09:00 ET; the recording began 11:28

    def setUp(self):
        S0 = C.parse_event(self.EV0)[1]
        self.paths = [C.settled_file(self.EV0),
                      os.path.join(C.DATA_ROOT, "sim", DAY, f"{self.EV0}.json")]
        C.write_json_atomic(self.paths[0], {
            "event_ticker": self.EV0, "fetched_rx": S0 + 430.0, "complete": True,
            "markets": market_objs(self.EV0, STRIKES1, "finalized",
                                   {f"{self.EV0}-T{k:.2f}": "no" for k in STRIKES1}, 70.0),
            "trades": {}, "index": [], "programs": programs_for(self.EV0, STRIKES1)})

    def tearDown(self):
        for p in self.paths:
            if os.path.exists(p):
                os.remove(p)

    def test_pre_recording_event_excluded(self):
        res = sim.run_day(DAY, workers=1)
        dark = next(r for r in res if r.get("event") == self.EV0)
        self.assertEqual(dark["coverage"]["book_trusted_frac"], 0.0)
        self.assertTrue(os.path.exists(self.paths[1]))
        ctx = report.build_context(DAY, now=TestReport.NOW)
        self.assertEqual([e["event"] for e in ctx["events"]], [E1, E2])
        self.assertEqual(ctx["agg"]["B_live300_pull|cons"]["n_events"], 2)
        self.assertEqual(ctx["health"]["events"]["simmed"], 2)
        self.assertNotIn(self.EV0, [m["event"] for m in ctx["health"]["events"]["missing"]])


if __name__ == "__main__":
    unittest.main()
