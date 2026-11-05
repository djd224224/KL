#!/usr/bin/env python3
"""Live page for one IMM election night session event (Jack 2026-11-03:
"generate a simple dashboard to just track this event in realtime, that i
can monitor"). Read-only: it never places, amends or cancels anything.

Every POLL_SECS it reads, for EVENT: the markets (touch, last, volume), the
account's resting orders (the bot's by client order id, the rest are his)
and the account's positions -- three API reads. The bot's fills and its
position come from its own fills sink (run-logs/incentive-mm/fills_*.jsonl:
pos_after per fill); his position is the account's less the bot's. Session
P&L marks the bot's fills since the session start at each market's mid, split
FIFO into realized (matched round trips: each matched contract closed at a
profit, a loss or flat) and open (the unmatched contracts at mid);
once a minute the cumulative P&L curve is rebuilt from the session start on
Kalshi's 1-minute candles (each minute's closing mid).

    python imm_ens_watch.py [--event KXSENATESEATSD-27] [--port 8765]

then open http://127.0.0.1:8765/ .
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "run-logs", "incentive-mm")
# importing incentive_mm reads and writes its STATUS_DIR; keep this
# read-only watcher off the live bot's files
os.environ.setdefault("IMM_STATUS_DIR", os.path.join(tempfile.gettempdir(), "imm_ens_watch_status"))
os.makedirs(os.environ["IMM_STATUS_DIR"], exist_ok=True)
sys.path.insert(0, HERE)
import incentive_mm as imm  # noqa: E402

POLL_SECS = 5.0
_state: dict = {"data": None, "err": None}
_lock = threading.Lock()


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


_flat_marks: dict = {}                  # ticker -> (flatten epoch, mark cents)


def flattens(event: str) -> dict:
    """{ticker: epoch} the bot's own book was zeroed there (own_book_flatten.txt,
    pending or .done -- the file's time), for the event's markets."""
    out = {}
    for name in ("own_book_flatten.txt", "own_book_flatten.txt.done"):
        p = os.path.join(LOG_DIR, name)
        try:
            mt = os.path.getmtime(p)
            with open(p, encoding="utf-8") as fh:
                for ln in fh:
                    t = ln.split("#", 1)[0].strip()
                    if t.startswith(event + "-"):
                        out[t] = mt
        except OSError:
            continue
    return out


def load_flat_marks(client, event: str) -> None:
    """Each flattened market's mid at its flatten minute (Kalshi's 1-minute
    candles), once: the price the page closes the old position at."""
    series = event.split("-")[0]
    for t, ts in flattens(event).items():
        if t in _flat_marks and _flat_marks[t][0] == ts:
            continue
        r = client.get(f"/series/{series}/markets/{t}/candlesticks",
                       params={"start_ts": int(ts) - 600, "end_ts": int(ts) + 60, "period_interval": 1})
        mid = None
        for cnd in r.get("candlesticks") or []:
            b = (cnd.get("yes_bid") or {}).get("close_dollars")
            a = (cnd.get("yes_ask") or {}).get("close_dollars")
            if b is not None and a is not None and int(cnd["end_period_ts"]) <= ts + 60:
                mid = (_f(b) + _f(a)) * 50.0
        if mid is not None:
            _flat_marks[t] = (ts, mid)


def bot_fills(event: str) -> list:
    """The bot's own fills on the event, oldest first, from its fills sink --
    plus, for a market whose book was flattened by hand (own_book_flatten),
    a synthetic fill closing what it held then at that minute's mid."""
    rows = []
    for p in sorted(glob.glob(os.path.join(LOG_DIR, "fills_*.jsonl")))[-4:]:
        try:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    if event not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("event_ticker") == event:
                        rows.append(r)
        except OSError:
            continue
    rows.sort(key=lambda r: _f(r.get("ts")))
    for t, (ts, mark) in _flat_marks.items():
        if not t.startswith(event + "-"):
            continue
        before = [r for r in rows if r.get("ticker") == t and _f(r.get("ts")) <= ts]
        pos = _f(before[-1].get("pos_after")) if before else 0.0
        if abs(pos) > 1e-9:
            rows.append({"ts": ts, "ticker": t, "event_ticker": event, "count": abs(pos),
                         "our_book_side": "bid" if pos < 0 else "ask",
                         "yes_price_cents": round(mark, 2), "pos_after": 0.0,
                         "avg_after": None, "synthetic": "flattened by hand"})
    return sorted(rows, key=lambda r: _f(r.get("ts")))


def fifo(fills: list, mid) -> dict:
    """FIFO-match a market's session fills [(yes_delta, yes_px_cents), ...]:
    realized $ and matched contracts closed at a profit / loss / flat, then
    the unmatched contracts and their open P&L at `mid`."""
    lots: list = []                       # [signed qty, px], oldest first
    out = {"real": 0.0, "won": 0.0, "lost": 0.0, "flat": 0.0,
           "won_usd": 0.0, "lost_usd": 0.0}
    for d, px in fills:
        q = d
        while abs(q) > 1e-9 and lots and (lots[0][0] > 0) != (q > 0):
            lot = lots[0]
            m = min(abs(q), abs(lot[0]))
            pnl = ((px - lot[1]) if lot[0] > 0 else (lot[1] - px)) * m / 100.0
            out["real"] += pnl
            if pnl > 1e-9:
                out["won"] += m
                out["won_usd"] += pnl
            elif pnl < -1e-9:
                out["lost"] += m
                out["lost_usd"] += pnl
            else:
                out["flat"] += m
            lot[0] -= m if lot[0] > 0 else -m
            q -= m if q > 0 else -m
            if abs(lot[0]) < 1e-9:
                lots.pop(0)
        if abs(q) > 1e-9:
            lots.append([q, px])
    out["open_ct"] = sum(abs(l[0]) for l in lots)
    out["open"] = sum(l[0] * ((mid if mid is not None else l[1]) - l[1]) for l in lots) / 100.0
    return out


def paged(client, path: str, key: str, params: dict) -> list:
    out, cursor = [], None
    for _ in range(50):
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        r = client.get(path, params=q)
        out += r.get(key) or []
        cursor = r.get("cursor") or r.get("next_cursor")
        if not cursor or not r.get(key):
            break
    return out


def snapshot(client, event: str, start: datetime) -> dict:
    now = datetime.now(timezone.utc)
    markets = paged(client, "/markets", "markets", {"event_ticker": event, "limit": 200})
    orders = paged(client, "/portfolio/orders", "orders",
                   {"event_ticker": event, "status": "resting", "limit": 200})
    positions = paged(client, "/portfolio/positions", "market_positions",
                      {"event_ticker": event, "limit": 200})
    fills = bot_fills(event)
    prefix = imm.CLIENT_ORDER_PREFIX + "-"

    rows = {}
    for m in markets:
        t = m["ticker"]
        bid, ask = _f(m.get("yes_bid_dollars")) * 100, _f(m.get("yes_ask_dollars")) * 100
        rows[t] = {"ticker": t, "strike": m.get("yes_sub_title") or t.rsplit("-", 1)[-1],
                   "bid": round(bid) if bid else None, "ask": round(ask) if ask else None,
                   "last": round(_f(m.get("last_price_dollars")) * 100) or None,
                   "vol24": round(_f(m.get("volume_24h_fp") or m.get("volume_24h"))),
                   "bot_bids": [], "bot_asks": [], "his_orders": 0,
                   "acct_pos": 0.0, "bot_pos": 0.0, "bot_avg": None,
                   "sess_pnl": 0.0, "sess_ct": 0.0, "_sess": []}
    for o in orders:
        r = rows.get(o.get("ticker"))
        if r is None:
            continue
        if str(o.get("client_order_id", "")).startswith(prefix):
            px = round(_f(o.get("yes_price_dollars")) * 100)
            n = round(_f(o.get("remaining_count_fp") or o.get("remaining_count")))
            (r["bot_bids"] if o.get("book_side") == "bid" else r["bot_asks"]).append([px, n])
        else:
            r["his_orders"] += 1
    for p in positions:
        r = rows.get(p.get("ticker"))
        if r is not None:
            r["acct_pos"] = _f(p.get("position_fp") or p.get("position"))

    start_ts = start.timestamp()
    recent = []
    tot = {"bot_sess_pnl": 0.0, "bot_sess_ct": 0.0, "bot_sess_fills": 0, "real": 0.0,
           "open": 0.0, "won": 0.0, "lost": 0.0, "flat": 0.0, "won_usd": 0.0,
           "lost_usd": 0.0, "open_ct": 0.0}
    for f in fills:
        r = rows.get(f.get("ticker"))
        if r is None:
            continue
        r["bot_pos"] = _f(f.get("pos_after"))
        r["bot_avg"] = f.get("avg_after")
        if _f(f.get("ts")) < start_ts:
            continue
        n = _f(f.get("count"))
        d = n if f.get("our_book_side") == "bid" else -n
        px = _f(f.get("yes_price_cents"))
        r["_sess"].append((d, px))
        if not f.get("synthetic"):          # a hand flatten is not a bot fill
            r["sess_ct"] += n
            tot["bot_sess_ct"] += n
            tot["bot_sess_fills"] += 1
        recent.append({"ts": datetime.fromtimestamp(_f(f.get("ts")), timezone.utc).strftime("%H:%M:%S"),
                       "strike": r["strike"] + (" (flattened by hand, at mid)" if f.get("synthetic") else ""),
                       "side": "BUY YES" if d > 0 else "SELL YES",
                       "n": round(n, 1), "px": round(px), "pos": round(_f(f.get("pos_after")))})
    for r in rows.values():
        mid = ((r["bid"] + r["ask"]) / 2.0 if r["bid"] and r["ask"]
               else r["last"] or r["bid"] or r["ask"])
        r["mid"] = mid
        fx = fifo(r.pop("_sess"), mid)
        pnl = fx["real"] + fx["open"]
        r["sess_pnl"] = round(pnl, 2)
        tot["bot_sess_pnl"] += pnl
        for k in ("real", "open", "won", "lost", "flat", "won_usd", "lost_usd", "open_ct"):
            r[k] = round(fx[k], 2)
            tot[k] += fx[k]
        r["his_pos"] = round(r["acct_pos"] - r["bot_pos"])
        r["bot_avg"] = round(_f(r["bot_avg"]), 1) if abs(r["bot_pos"]) > 0.5 else None
        r["bot_pos"] = round(r["bot_pos"])
        r["acct_pos"] = round(r["acct_pos"])
        r["bot_bids"].sort(reverse=True)
        r["bot_asks"].sort()
    tot["his_pos"] = sum(r["his_pos"] for r in rows.values())
    tot = {k: round(v, 2) if isinstance(v, float) else v for k, v in tot.items()}

    def strike_key(r):
        try:
            return float(r["ticker"].rsplit("-", 1)[-1].lstrip("T"))
        except ValueError:
            return 0.0
    return {"event": event, "now": now.isoformat(timespec="seconds"),
            "session_start": start.isoformat(), "session_end": imm.ENS_END.isoformat(),
            "live": imm.ENS_START <= now < imm.ENS_END,
            "skip": sorted(imm.ENS_SKIP_MARKETS) if hasattr(imm, "ENS_SKIP_MARKETS") else [],
            "rows": sorted(rows.values(), key=strike_key), "totals": tot,
            "recent": list(reversed(recent))[:40]}


_candles: dict = {}                     # ticker -> (fetched epoch, {minute end: mid})


def minute_mids(client, event: str, t: str, st: float) -> dict:
    """{minute end epoch: closing mid cents} for `t` from the minute ending at
    `st` on (Kalshi's 1-minute candles), cached for 50 s."""
    hit = _candles.get(t)
    if hit and time.time() - hit[0] < 50:
        return hit[1]
    r = client.get(f"/series/{event.split('-')[0]}/markets/{t}/candlesticks",
                   params={"start_ts": int(st) - 60, "end_ts": int(time.time()), "period_interval": 1})
    mids = {}
    for cnd in r.get("candlesticks") or []:
        b = (cnd.get("yes_bid") or {}).get("close_dollars")
        a = (cnd.get("yes_ask") or {}).get("close_dollars")
        if b is not None and a is not None:
            mids[int(cnd["end_period_ts"])] = (_f(b) + _f(a)) * 50.0
    _candles[t] = (time.time(), mids)
    return mids


def curve(client, event: str, start: datetime, by_t: dict, init: dict) -> tuple:
    """([[minute end epoch, realized $, open $], ...], {ticker: same}): at
    each minute since `start`, each market's starting lot (`init`: {ticker:
    (position, mark)}) and fills so far (`by_t`: {ticker: [(ts, yes delta,
    yes cents)]}) FIFO-matched (realized), the rest at that minute's mid."""
    st = int(start.timestamp())
    now = int(time.time())
    minutes = [m for m in range(st + 60, now + 60, 60) if m <= now]
    tot = {m: [0.0, 0.0] for m in minutes}
    by: dict = {}
    for t in sorted(set(by_t) | set(init)):
        fl, mids = by_t.get(t, []), minute_mids(client, event, t, st)
        lots = [init[t]] if t in init else []
        mid, one = None, []
        for m in minutes:
            mid = mids.get(m, mid)
            fx = fifo(lots + [(d, px) for ts, d, px in fl if ts <= m], mid)
            tot[m][0] += fx["real"]
            tot[m][1] += fx["open"]
            one.append([m, round(fx["real"], 2), round(fx["open"], 2)])
        by[t] = one
    return [[m, round(v[0], 2), round(v[1], 2)] for m, v in sorted(tot.items())], by


def pnl_series(client, event: str, start: datetime) -> tuple:
    """The bot's curve (curve): its session fills, flattens included."""
    st = int(start.timestamp())
    by_t: dict = {}
    for f in bot_fills(event):
        if _f(f.get("ts")) >= st:
            n = _f(f.get("count"))
            d = n if f.get("our_book_side") == "bid" else -n
            by_t.setdefault(f["ticker"], []).append((_f(f.get("ts")), d, _f(f.get("yes_price_cents"))))
    return curve(client, event, start, by_t, {})


# THE WHOLE ACCOUNT (Jack 2026-11-04: "add a toggle that shows this data for
# the whole event and markt across all trades, not just this bots"): every
# account fill on the event since the start (the bot's and his), each market
# opening with the account's position at the start marked at that minute's
# mid, so its P&L is since the start too.
_acct: dict = {"fills": {}, "loaded": set(), "max_ts": 0.0}


def _acct_row(f: dict) -> tuple:
    ts = f.get("ts")
    if not isinstance(ts, (int, float)) or ts < 1e9:
        ts = datetime.fromisoformat(str(f.get("created_time")).replace("Z", "+00:00")).timestamp()
    n = _f(f.get("count_fp") or f.get("count"))
    d = n if (f.get("action") == "buy") == (f.get("side") == "yes") else -n
    return (float(ts), f.get("ticker") or f.get("market_ticker"), d, _f(f.get("yes_price_dollars")) * 100.0)


def load_acct_fills(client, event: str, tickers: list, st: float) -> None:
    """Each market's account fills since `st` once, then the account's new
    fills since the last seen (one read), the event's kept."""
    def add(f):
        fid = f.get("fill_id") or f.get("trade_id")
        row = _acct_row(f)
        if fid and str(row[1]).startswith(event + "-"):
            _acct["fills"][fid] = row
            _acct["max_ts"] = max(_acct["max_ts"], row[0])
    for t in tickers:
        if t not in _acct["loaded"]:
            for f in paged(client, "/portfolio/fills", "fills", {"ticker": t, "min_ts": int(st), "limit": 200}):
                add(f)
            _acct["loaded"].add(t)
    for f in paged(client, "/portfolio/fills", "fills",
                   {"min_ts": int(max(st, _acct["max_ts"] - 30)), "limit": 200}):
        add(f)


def acct_view(client, event: str, start: datetime, d: dict, refresh_series: bool) -> dict:
    """The page's data for the whole account, in the bot view's shape."""
    st = start.timestamp()
    load_acct_fills(client, event, [r["ticker"] for r in d["rows"]], st)
    by_t: dict = {}
    for fid, (ts, t, dl, px) in _acct["fills"].items():
        if ts >= st:
            by_t.setdefault(t, []).append((ts, dl, px))
    for v in by_t.values():
        v.sort()
    init, rows = {}, {}
    keys = ("real", "open", "won", "lost", "flat", "won_usd", "lost_usd", "open_ct")
    tot = {k: 0.0 for k in keys + ("bot_sess_pnl", "bot_sess_ct")}
    tot["bot_sess_fills"] = sum(len(v) for v in by_t.values())
    for r in d["rows"]:
        t = r["ticker"]
        fl = by_t.get(t, [])
        pos0 = r["acct_pos"] - sum(x[1] for x in fl)
        mid0 = None
        if fl or abs(pos0) >= 1e-9:
            mids = minute_mids(client, event, t, st)
            mid0 = mids.get(int(st)) or (mids[min(mids)] if mids else None)
        lots = [(pos0, mid0)] if abs(pos0) >= 1e-9 and mid0 is not None else []
        if lots:
            init[t] = lots[0]
        fx = fifo(lots + [(dl, px) for _, dl, px in fl], r["mid"])
        ct = sum(abs(x[1]) for x in fl)
        rows[t] = {"bot_pos": r["acct_pos"], "bot_avg": None,
                   "sess_pnl": round(fx["real"] + fx["open"], 2), "sess_ct": ct,
                   **{k: round(fx[k], 2) for k in keys}}
        tot["bot_sess_pnl"] += fx["real"] + fx["open"]
        tot["bot_sess_ct"] += ct
        for k in keys:
            tot[k] += fx[k]
    if refresh_series or "acct_series" not in _state:
        _state["acct_series"], _state["acct_by"] = curve(client, event, start, by_t, init)
    now_i = int(time.time())
    hist = _state.get("acct_by") or {}
    names = {r["ticker"]: r["strike"] for r in d["rows"]}
    bot_ids = {f.get("fill_id") for f in bot_fills(event) if f.get("fill_id")}
    recent = sorted(((ts, fid, t, dl, px) for fid, (ts, t, dl, px) in _acct["fills"].items()
                     if ts >= st), reverse=True)[:40]
    allf = sorted((ts, dl) for v in by_t.values() for ts, dl, px in v)
    fs = {"total": [], "bought": [], "sold": []}
    for m in list(range(int(st), now_i, 60)) + [now_i]:
        b = sum(dl for ts, dl in allf if ts <= m and dl > 0)
        a = -sum(dl for ts, dl in allf if ts <= m and dl < 0)
        fs["total"].append([m, round(a + b, 1)])
        fs["bought"].append([m, round(b, 1)])
        fs["sold"].append([m, round(a, 1)])
    return {"totals": {k: round(v, 2) for k, v in tot.items()}, "rows": rows,
            "series": (_state.get("acct_series") or [])
            + [[now_i, round(tot["real"], 2), round(tot["open"], 2)]],
            "series_by": {t: (hist.get(t) or []) + [[now_i, rows[t]["real"], rows[t]["open"]]]
                          for t in rows if t in hist},
            "fills_series": fs,
            "recent": [{"ts": datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S"),
                        "strike": names.get(t, t), "side": "BUY YES" if dl > 0 else "SELL YES",
                        "n": round(abs(dl), 1), "px": round(px),
                        "who": "bot" if fid in bot_ids else "you"}
                       for ts, fid, t, dl, px in recent]}


def fills_series(event: str, start: datetime) -> dict:
    """Cumulative bot fill contracts since `start`, one point a minute plus
    now: {"total": [[epoch, ct], ...], "bought": [...], "sold": [...]}
    (bought = YES bought on its bids, sold = YES sold on its asks)."""
    st = int(start.timestamp())
    now = int(time.time())
    fl = [(_f(f.get("ts")), _f(f.get("count")), f.get("our_book_side") == "bid")
          for f in bot_fills(event) if _f(f.get("ts")) >= st and not f.get("synthetic")]
    out = {"total": [], "bought": [], "sold": []}
    for m in list(range(st, now, 60)) + [now]:
        b = sum(n for ts, n, bid in fl if ts <= m and bid)
        a = sum(n for ts, n, bid in fl if ts <= m and not bid)
        out["total"].append([m, round(a + b, 1)])
        out["bought"].append([m, round(b, 1)])
        out["sold"].append([m, round(a, 1)])
    return out


def poller(event: str, start: datetime) -> None:
    client = imm._fair_reader_client()
    last_series = 0.0
    while True:
        try:
            load_flat_marks(client, event)
            d = snapshot(client, event, start)
            if time.time() - last_series >= 60:
                _state["series"], _state["series_by"] = pnl_series(client, event, start)
                last_series = time.time()
            # the live point: this refresh's P&L at the current mids
            now_i = int(time.time())
            d["series"] = (_state.get("series") or []) + [[now_i, round(d["totals"]["real"], 2),
                                                            round(d["totals"]["open"], 2)]]
            d["fills_series"] = fills_series(event, start)
            hist = _state.get("series_by") or {}
            d["series_by"] = {r["ticker"]: (hist.get(r["ticker"]) or []) + [[now_i, r["real"], r["open"]]]
                              for r in d["rows"] if r["ticker"] in hist or r["sess_ct"]}
            refresh = time.time() - _state.get("acct_series_at", 0.0) >= 60
            av = acct_view(client, event, start, d, refresh)
            if refresh:
                _state["acct_series_at"] = time.time()
            d["views"] = {"bot": {"totals": d["totals"], "series": d["series"],
                                  "series_by": d["series_by"], "fills_series": d["fills_series"],
                                  "recent": d["recent"],
                                  "rows": {r["ticker"]: {k: r[k] for k in (
                                      "bot_pos", "bot_avg", "real", "open", "sess_pnl", "won", "lost",
                                      "flat", "open_ct", "sess_ct")} for r in d["rows"]}},
                          "acct": av}
            with _lock:
                _state["data"], _state["err"] = d, None
        except Exception as e:  # noqa: BLE001 -- keep polling, show the error
            with _lock:
                _state["err"] = f"{type(e).__name__}: {e}"[:300]
        time.sleep(POLL_SECS)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Senate Seats Live</title>
<style>
:root{--real:#2563eb;--open:#f59e0b;--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--line:#e7e5e4;--card:#fff;--up:#15803d;--dn:#b91c1c;--hi:#fef3c7}
@media (prefers-color-scheme:dark){:root{--real:#60a5fa;--open:#fbbf24;--bg:#0c0a09;--fg:#e7e5e4;--mut:#a8a29e;--line:#292524;--card:#1c1917;--up:#4ade80;--dn:#f87171;--hi:#422006}}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
h1{font-size:18px;margin:0 0 4px}.mut{color:var(--mut)}.up{color:var(--up)}.dn{color:var(--dn)}
.tiles{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:120px}
.tile b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-weight:600;color:var(--mut);font-size:12px}td:first-child,th:first-child{text-align:left}
tr.skip td{opacity:.45}tr.pos td{background:var(--hi)}
.err{color:var(--dn);margin:8px 0}
.seg{display:inline-flex;margin-top:10px;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{font:inherit;padding:6px 12px;border:0;background:var(--card);color:var(--fg);cursor:pointer}
.seg button.on{background:var(--fg);color:var(--bg)}
</style></head><body>
<h1 id="h">Senate seats</h1><div class="mut" id="sub">loading...</div>
<div class="seg" role="group" aria-label="Whose trades"><button id="vbot" onclick="setView('bot')">Bot only</button><button id="vacct" onclick="setView('acct')">Whole account (bot + you)</button></div>
<div class="err" id="err"></div>
<div class="tiles" id="tiles"></div>
<div class="wrap" style="padding:8px 12px;margin-bottom:12px"><div class="mut" style="font-size:12px"><span class="who">Bot</span> cumulative P&amp;L since the session start, a bar a minute:
<span style="color:var(--real)">&#9632; realized</span> &nbsp;<span style="color:var(--open)">&#9632; open (at mid)</span> &nbsp;&#9679; total</div>
<svg id="chart" viewBox="0 0 800 200" preserveAspectRatio="none" style="width:100%;height:200px;display:block"></svg></div>
<div class="wrap" style="padding:8px 12px;margin-bottom:12px"><div class="mut" style="font-size:12px"><span class="who">Bot</span> cumulative fills since the session start (contracts):
<b style="color:var(--fg)">&#9644; total</b> &nbsp;<span style="color:var(--up)">&#9644; YES bought (bids)</span> &nbsp;<span style="color:var(--dn)">&#9644; YES sold (asks)</span></div>
<svg id="fchart" viewBox="0 0 800 170" preserveAspectRatio="none" style="width:100%;height:170px;display:block"></svg></div>
<div class="mut" style="font-size:12px;margin:0 0 6px">Each market, realized + open (same $ scale)</div>
<div id="small" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px;margin-bottom:12px"></div>
<div class="wrap"><table><thead><tr><th>Strike</th><th>Bid</th><th>Ask</th><th>Last</th><th>Vol 24h</th>
<th>Bot bids</th><th>Bot asks</th><th><span class="who">Bot</span> pos</th><th>Bot avg</th><th>Realized</th><th>Open (mid)</th><th><span class="who">Bot</span> P&amp;L (session)</th>
<th>Matched +/-/flat</th><th>Unmatched</th>
<th>Your pos</th><th>Your orders</th></tr></thead><tbody id="rows"></tbody></table></div>
<h2 style="font-size:15px;margin:16px 0 6px"><span class="who">Bot</span> fills since the session start</h2>
<div class="wrap"><table><thead><tr><th>Time (UTC)</th><th>Strike</th><th>Side</th><th>Qty</th><th>Price</th><th id="lastcol">Bot pos after</th></tr></thead>
<tbody id="fills"></tbody></table></div>
<script>
const $=id=>document.getElementById(id);
const money=v=>(v<0?'-$':'$')+Math.abs(v).toFixed(2);
const cls=v=>v>0?'up':v<0?'dn':'';
const lad=a=>a.map(x=>x[1]+'@'+x[0]).join(' ')||'<span class="mut">-</span>';
let view='bot';try{view=localStorage.getItem('ensView')||'bot'}catch(e){}
function setView(v){view=v;try{localStorage.setItem('ensView',v)}catch(e){};tick()}
async function tick(){
  try{
    const r=await fetch('/data',{cache:'no-store'});const j=await r.json();
    $('err').textContent=j.err||'';const d0=j.data;if(!d0)return;
    const V=(d0.views||{})[view]||{},acct=view==='acct';
    const d=Object.assign({},d0,{rows:d0.rows.map(r=>Object.assign({},r,(V.rows||{})[r.ticker]||{})),
      totals:Object.assign({},d0.totals,V.totals||{}),series:V.series||d0.series,series_by:V.series_by||d0.series_by,
      fills_series:V.fills_series||d0.fills_series,recent:V.recent||d0.recent});
    $('vbot').className=acct?'':'on';$('vacct').className=acct?'on':'';
    document.querySelectorAll('.who').forEach(e=>e.textContent=acct?'Account':'Bot');
    $('lastcol').textContent=acct?'Who':'Bot pos after';
    $('h').textContent=d.event;
    const end=new Date(d.session_end),now=new Date(d.now);
    const mins=Math.max(0,Math.round((end-now)/60000));
    $('sub').textContent=(d.live?'LIVE, ends in '+mins+' min':'not in session')+' | updated '+d.now.slice(11,19)+'Z';
    const t=d.totals;
    $('tiles').innerHTML=[
      [(acct?'Account':'Bot')+' P&L (session, at mid)',`<span class="${cls(t.bot_sess_pnl)}">${money(t.bot_sess_pnl)}</span>
        <div style="font-size:12px;font-weight:400"><span class="mut">realized</span> <span class="${cls(t.real)}">${money(t.real)}</span>
        &nbsp;<span class="mut">open</span> <span class="${cls(t.open)}">${money(t.open)}</span></div>`],
      ['Matched contracts',`${Math.round(t.won+t.lost+t.flat)}
        <div style="font-size:12px;font-weight:400"><span class="up">${Math.round(t.won)} profit (${money(t.won_usd)})</span>
        &nbsp;<span class="dn">${Math.round(t.lost)} loss (${money(t.lost_usd)})</span>${t.flat?` &nbsp;<span class="mut">${Math.round(t.flat)} flat</span>`:''}</div>`],
      ['Unmatched contracts',Math.round(t.open_ct)],
      [(acct?'Account':'Bot')+' fills / contracts',t.bot_sess_fills+' / '+Math.round(t.bot_sess_ct)],
      ['Your net YES (event)',t.his_pos],
      [(acct?'Account':'Bot')+' net YES (event)',d.rows.reduce((s,r)=>s+r.bot_pos,0)]
    ].map(([k,v])=>`<div class="tile"><span class="mut">${k}</span><b>${v}</b></div>`).join('');
    $('rows').innerHTML=d.rows.map(r=>{
      const skip=d.skip.includes(r.ticker);
      return `<tr class="${skip?'skip':(r.bot_pos?'pos':'')}"><td>${r.strike}${skip?' (not quoted)':''}</td>
      <td>${r.bid??'-'}</td><td>${r.ask??'-'}</td><td>${r.last??'-'}</td><td>${r.vol24.toLocaleString()}</td>
      <td>${lad(r.bot_bids)}</td><td>${lad(r.bot_asks)}</td>
      <td class="${cls(r.bot_pos)}">${r.bot_pos}</td><td>${r.bot_avg??'-'}</td>
      <td class="${cls(r.real)}">${money(r.real)}</td><td class="${cls(r.open)}">${money(r.open)}</td>
      <td class="${cls(r.sess_pnl)}"><b>${money(r.sess_pnl)}</b></td>
      <td><span class="up">${Math.round(r.won)}</span> / <span class="dn">${Math.round(r.lost)}</span> / <span class="mut">${Math.round(r.flat)}</span></td>
      <td>${Math.round(r.open_ct)}</td>
      <td class="${cls(r.his_pos)}">${r.his_pos}</td><td>${r.his_orders||'-'}</td></tr>`}).join('');
    $('fills').innerHTML=d.recent.map(f=>`<tr><td>${f.ts}</td><td>${f.strike}</td>
      <td class="${f.side==='BUY YES'?'up':'dn'}">${f.side}</td><td>${f.n}</td><td>${f.px}c</td><td>${f.who??f.pos}</td></tr>`).join('')
      ||'<tr><td colspan="6" class="mut">none yet</td></tr>';
    charts(d);
  }catch(e){$('err').textContent='page: '+e}
}
function range(s){let lo=0,hi=0;for(const [,a,b] of s){hi=Math.max(hi,Math.max(a,0)+Math.max(b,0));lo=Math.min(lo,Math.min(a,0)+Math.min(b,0))}return [lo,hi]}
function bars(svg,s,W,H,lo,hi,label){
  if(!s||!s.length){svg.innerHTML='';return}
  const L=label?52:6,R=label?64:48,T=8,B=label?24:6;
  let [y0,y1]=lo!=null?[lo,hi]:range(s);if(y1-y0<1){y1+=0.5;y0-=0.5}
  const n=s.length,pw=(W-L-R)/n,bw=Math.max(1,pw*0.78);
  const Y=y=>T+(y1-y)/(y1-y0)*(H-T-B);
  const et=t=>new Date(t*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'});
  let g='';
  if(label){for(const v of [y0,y1])g+=`<line x1="${L}" x2="${W-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${L-6}" y="${Y(v)+4}" text-anchor="end" font-size="11" fill="var(--mut)">${money(v)}</text>`;
    for(const i of [0,Math.floor((n-1)/2),n-1])g+=`<text x="${L+pw*i+pw/2}" y="${H-6}" text-anchor="middle" font-size="11" fill="var(--mut)">${et(s[i][0])}</text>`}
  s.forEach(([t,a,b],i)=>{const x=L+pw*i+(pw-bw)/2;let up=0,dn=0;
    for(const [v,col] of [[a,'var(--real)'],[b,'var(--open)']]){if(!v)continue;
      const base=v>0?up:dn,top=base+v;g+=`<rect x="${x.toFixed(1)}" y="${Math.min(Y(base),Y(top)).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.abs(Y(top)-Y(base)).toFixed(1)}" fill="${col}"><title>${et(t)}: realized ${money(a)}, open ${money(b)}</title></rect>`;
      if(v>0)up=top;else dn=top}});
  g+=`<line x1="${L}" x2="${W-R}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--mut)" stroke-width="1"/>`;
  const [,la,lb]=s[n-1],tot=la+lb,col=tot>=0?'var(--up)':'var(--dn)';
  g+=`<circle cx="${L+pw*(n-1)+pw/2}" cy="${Y(tot)}" r="3" fill="var(--fg)"/><text x="${W-R+4}" y="${Y(tot)+4}" font-size="12" font-weight="600" fill="${col}">${money(tot)}</text>`;
  svg.innerHTML=g;
}
function lines(svg,set,W,H){
  const all=set.flatMap(x=>x.s);if(all.length<2){svg.innerHTML='';return}
  const L=52,R=60,T=8,B=24,x0=Math.min(...all.map(p=>p[0])),x1=Math.max(...all.map(p=>p[0]));
  const y1=Math.max(1,...all.map(p=>p[1]));
  const X=x=>L+(x-x0)/(x1-x0||1)*(W-L-R),Y=y=>T+(y1-y)/y1*(H-T-B);
  const et=t=>new Date(t*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'});
  let g='';for(const v of [0,y1/2,y1])g+=`<line x1="${L}" x2="${W-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--line)"/><text x="${L-6}" y="${Y(v)+4}" text-anchor="end" font-size="11" fill="var(--mut)">${Math.round(v).toLocaleString()}</text>`;
  for(const t of [x0,(x0+x1)/2,x1])g+=`<text x="${X(t)}" y="${H-6}" text-anchor="middle" font-size="11" fill="var(--mut)">${et(t)}</text>`;
  for(const {s,col,w} of set){const last=s[s.length-1];
    g+=`<polyline points="${s.map(p=>X(p[0]).toFixed(1)+','+Y(p[1]).toFixed(1)).join(' ')}" fill="none" stroke="${col}" stroke-width="${w}" vector-effect="non-scaling-stroke"/>`+
       `<text x="${X(last[0])+5}" y="${Y(last[1])+4}" font-size="11" font-weight="600" fill="${col}">${Math.round(last[1]).toLocaleString()}</text>`}
  svg.innerHTML=g;
}
function charts(d){
  bars($('chart'),d.series,800,200,null,null,true);
  const f=d.fills_series;if(f)lines($('fchart'),[{s:f.bought,col:'var(--up)',w:1.25},{s:f.sold,col:'var(--dn)',w:1.25},{s:f.total,col:'var(--fg)',w:2}],800,170);
  const by=d.series_by||{},names=Object.fromEntries(d.rows.map(r=>[r.ticker,r.strike]));
  const rs=Object.values(by).map(range);
  const lo=Math.min(0,...rs.map(r=>r[0])),hi=Math.max(0,...rs.map(r=>r[1]));
  const ks=Object.keys(by).sort();
  const box=$('small');
  if(box.dataset.k!==ks.join()){box.dataset.k=ks.join();box.innerHTML=ks.map(k=>`<div class="tile" style="min-width:0"><span class="mut">${names[k]||k}</span><svg data-t="${k}" viewBox="0 0 300 90" preserveAspectRatio="none" style="width:100%;height:90px;display:block"></svg></div>`).join('')||'<span class="mut">no bot fills yet</span>'}
  for(const k of ks)bars(box.querySelector(`svg[data-t="${k}"]`),by[k],300,90,lo,hi,false);
}
tick();setInterval(tick,3000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/data"):
            with _lock:
                body = json.dumps({"data": _state["data"], "err": _state["err"]}).encode()
            ctype = "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", default="KXSENATESEATSD-27")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    start = imm.ENS_START if a.event in getattr(imm, "ENS_EVENTS", ()) else datetime.now(timezone.utc)
    threading.Thread(target=poller, args=(a.event, start), daemon=True).start()
    print(f"imm_ens_watch: http://127.0.0.1:{a.port}/ ({a.event})", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
