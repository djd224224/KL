#!/usr/bin/env python3
"""Live page for the IMM post-vote dead books (PVD, 2026-11-04; Jack: "once
live, build simple dashboard to track, refreshing every 5min ... emphasis on
trading PnL vs estimated rewards"). Read-only: it never places, amends or
cancels anything.

Every POLL_SECS (300) it reads, for the markets listed in
run-logs/incentive-mm/post_vote_dead_books.json:
  * the markets (touch, last trade) -- Kalshi's public /markets;
  * the account's resting orders -- the bot's by client order id;
  * each quoted market's orderbook -- public.
EST REWARDS: our share of each book with what we actually rest (the bot's own
estimate_reward_share: both sides must reach the target or the snapshot pays
nobody) x the market's pool $/day, integrated poll to poll from the go-live
(GO_LIVE; the stretch before the first read at the first read's rate).
Kalshi has paid ~1.15x the bot's estimate; the page shows the raw estimate.
TRADING P&L: the bot's fills since GO_LIVE from its fills sink
(fills_*.jsonl), FIFO per market: realized on matched contracts, open on the
rest marked at the LAST TRADE (a dead book's mid -- 1c x 99c -> 50c -- is
no mark), falling back to the mid.

    python imm_pvd_watch.py [--port 8771]

then open http://127.0.0.1:8771/ .
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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "run-logs", "incentive-mm")
LIST_FILE = os.path.join(LOG_DIR, "post_vote_dead_books.json")
# a NEW wave (the market-wave playbook): PVD_WATCH_DIR for a fresh history and
# PVD_WATCH_GO_LIVE (ISO) for its go-live; defaults = the 11/04 election wave
HIST_DIR = os.environ.get("PVD_WATCH_DIR") or os.path.join(HERE, "run-logs", "pvd-watch")
HIST_FILE = os.path.join(HIST_DIR, "history.json")
# importing incentive_mm reads and writes its STATUS_DIR; keep this
# read-only watcher off the live bot's files
os.environ.setdefault("IMM_STATUS_DIR", os.path.join(tempfile.gettempdir(), "imm_pvd_watch_status"))
os.makedirs(os.environ["IMM_STATUS_DIR"], exist_ok=True)
sys.path.insert(0, HERE)
import incentive_mm as imm  # noqa: E402

PUBLIC = "https://api.elections.kalshi.com/trade-api/v2"
GO_LIVE = (datetime.fromisoformat(os.environ["PVD_WATCH_GO_LIVE"].replace("Z", "+00:00"))
           if os.environ.get("PVD_WATCH_GO_LIVE")
           else datetime(2026, 11, 4, 13, 49, 12, tzinfo=timezone.utc))   # run 794bc9fc loaded the list
POLL_SECS = 300.0
_state: dict = {"data": None, "err": None, "next": None}
_lock = threading.Lock()
_sess = threading.local()


def _f(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _get(path: str, params: dict | None = None) -> dict:
    """A public read, patient with 429s (the public limit is per IP and shared
    with whatever else runs on this machine): up to ~75 s of backoff, then
    raise -- the caller falls back to its last good read."""
    s = getattr(_sess, "s", None)
    if s is None:
        s = _sess.s = requests.Session()
    r = None
    for a in range(9):
        try:
            r = s.get(PUBLIC + path, params=params, timeout=20)
        except requests.RequestException:
            time.sleep(2 + 2 * a)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 + 2 * a)
            continue
        r.raise_for_status()
        return r.json()
    if r is not None:
        r.raise_for_status()
    raise RuntimeError(f"no response for {path}")


SCAN_FILE = os.path.join(HERE, "run-logs", "pvd-scan", "last_scan.json")   # pvd_scan.py's last run


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")) if s else None
    except ValueError:
        return None


_mk_cache: dict = {}          # ticker -> last good market record
_book_cache: dict = {}        # ticker -> last good orderbook
_stale = {"markets": 0, "books": 0}


def load_list() -> dict:
    """Listed markets, plus the ones stopped by hand (top-level "stopped",
    which the bot never reads) so their history stays on the page."""
    with open(LIST_FILE, encoding="utf-8") as fh:
        j = json.load(fh)
    out = dict(j.get("markets") or {})
    for t, s in (j.get("stopped") or {}).items():
        if t not in out and isinstance(s, dict):
            e = dict(s.get("entry") or {})
            e["stopped_at"] = s.get("at")
            out[t] = e
    return out


def load_hist() -> dict:
    try:
        with open(HIST_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"last_ts": None, "cum": {}, "series": []}


def save_hist(h: dict) -> None:
    os.makedirs(HIST_DIR, exist_ok=True)
    tmp = HIST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(h, fh)
    os.replace(tmp, HIST_FILE)


def bot_fills(tickers: set, start_ts: float) -> list:
    rows = []
    for p in sorted(glob.glob(os.path.join(LOG_DIR, "fills_*.jsonl")))[-3:]:
        try:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("ticker") in tickers and _f(r.get("ts")) >= start_ts:
                        rows.append(r)
        except OSError:
            continue
    return sorted(rows, key=lambda r: _f(r.get("ts")))


def fifo(fills: list, mark) -> dict:
    """[(yes_delta, px_cents)] -> realized $, open $ at `mark`, open contracts."""
    lots: list = []
    real = 0.0
    for d, px in fills:
        q = d
        while abs(q) > 1e-9 and lots and (lots[0][0] > 0) != (q > 0):
            lot = lots[0]
            m = min(abs(q), abs(lot[0]))
            real += ((px - lot[1]) if lot[0] > 0 else (lot[1] - px)) * m / 100.0
            lot[0] -= m if lot[0] > 0 else -m
            q -= m if q > 0 else -m
            if abs(lot[0]) < 1e-9:
                lots.pop(0)
        if abs(q) > 1e-9:
            lots.append([q, px])
    net = sum(l[0] for l in lots)
    opn = sum(l[0] * ((mark if mark is not None else l[1]) - l[1]) for l in lots) / 100.0
    cost = sum(abs(l[0]) * (l[1] if l[0] > 0 else 100 - l[1]) for l in lots) / 100.0
    return {"real": real, "open": opn, "net": net, "cost": cost}


def read_markets(tickers: list) -> dict:
    """Batch market reads; a batch that still fails keeps its last good
    records (counted in _stale) instead of failing the whole poll."""
    out = {}
    _stale["markets"] = 0
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        try:
            j = _get("/markets", {"tickers": ",".join(chunk), "limit": 1000})
            for m in j.get("markets") or []:
                out[m["ticker"]] = _mk_cache[m["ticker"]] = m
        except Exception:  # noqa: BLE001
            for t in chunk:
                if t in _mk_cache:
                    out[t] = _mk_cache[t]
            _stale["markets"] += len(chunk)
        time.sleep(0.5)
    return out


def read_orders(client) -> list:
    out, cursor = [], None
    for _ in range(20):
        q = {"status": "resting", "limit": 1000}
        if cursor:
            q["cursor"] = cursor
        r = client.get("/portfolio/orders", params=q)
        out += r.get("orders") or []
        cursor = r.get("cursor")
        if not cursor or not r.get("orders"):
            break
    return out


def read_book(t: str):
    try:
        ob = _get(f"/markets/{t}/orderbook")
        _book_cache[t] = ob
        time.sleep(0.35)
        return t, ob
    except Exception:  # noqa: BLE001
        _stale["books"] += 1
        return t, _book_cache.get(t)


def poll(client, hist: dict) -> dict:
    now = datetime.now(timezone.utc)
    lst = load_list()
    tickers = sorted(lst)
    mk = read_markets(tickers)
    prefix = imm.CLIENT_ORDER_PREFIX + "-"
    own: dict = {t: [] for t in tickers}
    for o in read_orders(client):
        t = o.get("ticker")
        if t in own and str(o.get("client_order_id", "")).startswith(prefix):
            px = round(_f(o.get("yes_price_dollars")) * 100)
            n = _f(o.get("remaining_count_fp") or o.get("remaining_count"))
            own[t].append((o.get("book_side") or ("bid" if o.get("side") == "yes" else "ask"), px, n))
    quoted = [t for t in tickers if own[t]]
    _stale["books"] = 0
    with ThreadPoolExecutor(3) as ex:
        books = dict(ex.map(read_book, quoted))

    rows, est_rate = {}, 0.0
    for t in tickers:
        e, m = lst[t], mk.get(t) or {}
        stop = _ts(e.get("stop"))
        added = str(e.get("added") or "")
        by_scanner = added.startswith("scan 20")      # pvd_scan.py stamps "scan YYYY-MM-DDTHH:MMZ (...)"
        src = ("stopped by hand" if e.get("stopped_at") else
               "extended" if e.get("extended") else
               "scan relit" if by_scanner and "relit" in added else
               "scan new" if by_scanner else "11/04 list")
        bid = round(_f(m.get("yes_bid_dollars")) * 100) or None
        ask = round(_f(m.get("yes_ask_dollars")) * 100) or None
        last = _f(m.get("last_price_dollars")) * 100 or None
        frac, sides = 0.0, 0
        ob = books.get(t)
        if ob:
            y, n = imm.orderbook_levels(ob)
            frac, sides = imm.estimate_reward_share(y, n, own[t], 1000.0, 0.5, own_in_book=True)
        rate = frac * _f(e.get("pool_day"))
        est_rate += rate
        rows[t] = {"ticker": t, "event": e.get("event"), "series": t.split("-")[0],
                   "sub": m.get("yes_sub_title") or "", "status": m.get("status"),
                   "bid": bid, "ask": ask, "last": round(last, 1) if last else None,
                   "limits": [e.get("bid_max"), e.get("ask_min")],
                   "our_bid": max((p for s, p, q in own[t] if s == "bid"), default=None),
                   "our_ask": min((p for s, p, q in own[t] if s == "ask"), default=None),
                   "rest_ct": round(sum(q for _s, _p, q in own[t])),
                   "share": round(frac, 4), "sides": sides, "rate": round(rate, 2),
                   "pool": _f(e.get("pool_day")), "kind": e.get("kind") or "dead",
                   "size": int(e.get("size") or 40),
                   "bsize": int(e.get("bid_size") or e.get("size") or 40),
                   "asize": int(e.get("ask_size") or e.get("size") or 40),
                   "stop": e.get("stopped_at") or e.get("stop"),
                   "active": bool(stop and stop > now and not e.get("stopped_at")), "src": src,
                   "period": e.get("period_start") or "wave-1104",
                   "hrs_left": round(max(0.0, (stop - now).total_seconds() / 3600.0), 2) if stop else 0.0}

    # est rewards: integrate each market's rate since the previous read
    prev = hist.get("last_ts")
    dt = (now.timestamp() - (prev if prev else GO_LIVE.timestamp())) / 86400.0
    cum = hist.setdefault("cum", {})
    # banked THIS program period (Kalshi's $1 floor counts per period): resets
    # when the scanner relists a market on a new period (its period_start moves)
    pcum = hist.setdefault("pcum", {})
    for t, r in rows.items():
        add = r["rate"] * max(dt, 0.0)
        cum[t] = cum.get(t, 0.0) + add
        pk, pv = pcum.get(t) or [r["period"], cum[t] - add]
        if pk != r["period"]:
            pk, pv = r["period"], 0.0
        pcum[t] = [pk, pv + add]
        r["period_est"] = round(pcum[t][1], 2)
        r["proj_period"] = round(pcum[t][1] + (r["rate"] * r["hrs_left"] / 24.0 if r["active"] else 0.0), 2)
    hist["last_ts"] = now.timestamp()

    # trading: the bot's fills since the go-live, FIFO, marked at the last trade
    fills = bot_fills(set(tickers), GO_LIVE.timestamp())
    per: dict = {t: [] for t in tickers}
    recent = []
    for f in fills:
        n = _f(f.get("count"))
        d = n if f.get("our_book_side") == "bid" else -n
        per[f["ticker"]].append((d, _f(f.get("yes_price_cents"))))
        recent.append({"ts": datetime.fromtimestamp(_f(f.get("ts")), timezone.utc).strftime("%H:%M:%S"),
                       "ticker": f["ticker"], "side": "BUY YES" if d > 0 else "SELL YES",
                       "n": round(n, 1), "px": round(_f(f.get("yes_price_cents")))})
    # fills over time: contracts per 5-min bucket since go-live, YES bought
    # (our bids filled) and YES sold (our asks filled)
    b0 = GO_LIVE.timestamp(); step = 300.0
    nb = int((now.timestamp() - b0) // step) + 1
    fb_bid, fb_ask = [0.0] * nb, [0.0] * nb
    for f in fills:
        i = int((_f(f.get("ts")) - b0) // step)
        if 0 <= i < nb:
            (fb_bid if f.get("our_book_side") == "bid" else fb_ask)[i] += _f(f.get("count"))
    fill_series = {"t0": b0, "step": step, "bid": [round(v, 1) for v in fb_bid],
                   "ask": [round(v, 1) for v in fb_ask]}
    tot = {"real": 0.0, "open": 0.0, "cost": 0.0, "fills": len(fills),
           "fill_ct": sum(_f(f.get("count")) for f in fills)}
    for t, r in rows.items():
        mark = r["last"] if r["last"] is not None else (
            (r["bid"] + r["ask"]) / 2 if r["bid"] and r["ask"] else None)
        fx = fifo(per[t], mark)
        r.update(real=round(fx["real"], 2), open=round(fx["open"], 2),
                 trade=round(fx["real"] + fx["open"], 2), pos=round(fx["net"]),
                 est=round(cum.get(t, 0.0), 2), n_fills=len(per[t]))
        r["net"] = round(r["trade"] + r["est"], 2)
        tot["real"] += fx["real"]
        tot["open"] += fx["open"]
        tot["cost"] += fx["cost"]
    tot["trade"] = tot["real"] + tot["open"]
    tot["est"] = sum(cum.get(t, 0.0) for t in tickers)
    tot["net"] = tot["trade"] + tot["est"]
    tot["rate"] = est_rate
    act = [r for r in rows.values() if r["active"]]
    tot["quoted"] = sum(1 for r in act if r["rest_ct"])
    tot["listed"] = len(act)
    tot["ended"] = len(rows) - len(act)
    tot["two_sided"] = sum(1 for r in act if r["sides"] == 2)
    tot["rest_ct"] = sum(r["rest_ct"] for r in rows.values())
    tot["period_ok"] = sum(1 for r in act if r["proj_period"] >= 1.0)
    tot["period_short"] = sum(1 for r in act if r["proj_period"] < 1.0)
    stops = sorted({r["stop"] for r in act if r["stop"]})
    end = _ts(stops[0]) if stops else None
    hrs_left = max(0.0, (end - now).total_seconds() / 3600.0) if end else 0.0
    tot["proj_est"] = tot["est"] + sum(r["rate"] * r["hrs_left"] / 24.0 for r in act)
    stop_mix: dict = {}
    for r in act:
        stop_mix[r["stop"]] = stop_mix.get(r["stop"], 0) + 1
    srcs: dict = {}
    for r in rows.values():
        k = srcs.setdefault(r["src"], {"src": r["src"], "n": 0, "active": 0, "quoted": 0, "est": 0.0,
                                       "trade": 0.0, "rate": 0.0, "fills": 0})
        k["n"] += 1
        k["active"] += 1 if r["active"] else 0
        k["quoted"] += 1 if r["rest_ct"] else 0
        for f in ("est", "trade", "rate"):
            k[f] += r[f]
        k["fills"] += r["n_fills"]
    for k in srcs.values():
        k["net"] = round(k["est"] + k["trade"], 2)
        for f in ("est", "trade", "rate"):
            k[f] = round(k[f], 2)
    try:
        with open(SCAN_FILE, encoding="utf-8") as fh:
            scan = json.load(fh)
        scan.pop("taken", None)
    except (OSError, ValueError):
        scan = None
    kinds: dict = {}
    for r in rows.values():
        k = kinds.setdefault(r["kind"], {"kind": r["kind"], "n": 0, "quoted": 0, "est": 0.0, "trade": 0.0,
                                         "real": 0.0, "open": 0.0, "rate": 0.0, "fills": 0, "pos_mkts": 0})
        k["n"] += 1 if r["active"] else 0
        k["quoted"] += 1 if r["rest_ct"] else 0
        for f in ("est", "trade", "real", "open", "rate"):
            k[f] += r[f]
        k["fills"] += r["n_fills"]
        k["pos_mkts"] += 1 if r["pos"] else 0
    for k in kinds.values():
        k["net"] = k["est"] + k["trade"]
        for f in ("est", "trade", "real", "open", "rate", "net"):
            k[f] = round(k[f], 2)
    kd, kl = kinds.get("dead", {}), kinds.get("live", {})
    hist.setdefault("series", []).append(
        [round(now.timestamp()), round(tot["est"], 2), round(tot["trade"], 2), round(tot["net"], 2),
         kd.get("est", 0.0), kd.get("trade", 0.0), kl.get("est", 0.0), kl.get("trade", 0.0)])
    hist["series"] = hist["series"][-2000:]
    save_hist(hist)

    by: dict = {}
    for r in rows.values():
        b = by.setdefault(r["series"], {"series": r["series"], "n": 0, "quoted": 0, "est": 0.0,
                                        "trade": 0.0, "rate": 0.0, "fills": 0})
        b["n"] += 1 if r["active"] else 0
        b["quoted"] += 1 if r["rest_ct"] else 0
        b["est"] += r["est"]
        b["trade"] += r["trade"]
        b["rate"] += r["rate"]
        b["fills"] += r["n_fills"]
    for b in by.values():
        b["net"] = round(b["est"] + b["trade"], 2)
        for k in ("est", "trade", "rate"):
            b[k] = round(b[k], 2)
    return {"now": now.isoformat(), "go_live": GO_LIVE.isoformat(), "stale": dict(_stale),
            "end": end.isoformat() if end else None, "hrs_left": round(hrs_left, 2),
            "totals": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in tot.items()},
            "rows": sorted(rows.values(), key=lambda r: r["trade"]),
            "by_series": sorted(by.values(), key=lambda b: b["net"]),
            "by_kind": sorted(kinds.values(), key=lambda k: k["kind"]),
            "by_src": sorted(srcs.values(), key=lambda k: k["src"]), "scan": scan,
            "stop_mix": sorted(stop_mix.items()),
            "series": hist["series"], "fill_series": fill_series, "recent": recent[-40:][::-1]}


def poller() -> None:
    client = imm._fair_reader_client()
    hist = load_hist()
    while True:
        t0 = time.time()
        try:
            d = poll(client, hist)
            with _lock:
                _state["data"], _state["err"] = d, None
        except Exception as e:  # noqa: BLE001 -- keep polling, show the error
            with _lock:
                _state["err"] = f"{type(e).__name__}: {e}"[:300]
        nxt = t0 + POLL_SECS
        with _lock:
            _state["next"] = nxt
        time.sleep(max(5.0, nxt - time.time()))


PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dead Books Live</title>
<style>
:root{--dead:#7c3aed;--live:#0891b2;--est:#2563eb;--trade:#ea580c;--bg:#fafaf9;--fg:#1c1917;--mut:#78716c;--line:#e7e5e4;--card:#fff;--up:#15803d;--dn:#b91c1c;--hi:#fef3c7}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--dead:#a78bfa;--live:#22d3ee;--est:#60a5fa;--trade:#fb923c;--bg:#0c0a09;--fg:#e7e5e4;--mut:#a8a29e;--line:#292524;--card:#1c1917;--up:#4ade80;--dn:#f87171;--hi:#422006}}
:root[data-theme="dark"]{--dead:#a78bfa;--live:#22d3ee;--est:#60a5fa;--trade:#fb923c;--bg:#0c0a09;--fg:#e7e5e4;--mut:#a8a29e;--line:#292524;--card:#1c1917;--up:#4ade80;--dn:#f87171;--hi:#422006}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.4 system-ui,sans-serif}
h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:18px 0 6px}.mut{color:var(--mut)}.up{color:var(--up)}.dn{color:var(--dn)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px;margin:12px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 12px}
.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums}.tile small{font-size:12px;color:var(--mut)}
.tile.big b{font-size:28px}
.wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{font-weight:600;color:var(--mut);font-size:12px;cursor:pointer;user-select:none}td:first-child,th:first-child{text-align:left}
tr.pos td{background:var(--hi)}tr.off td{opacity:.5}
.err{color:var(--dn);margin:8px 0}.key{font-size:12px}
.tag{display:inline-block;padding:1px 6px;border-radius:9px;font-size:11px;font-weight:600;color:#fff}.tag.dead{background:var(--dead)}.tag.live{background:var(--live)}
</style></head><body>
<h1>Post-vote dead books</h1><div class="mut" id="sub">loading...</div>
<div class="mut" id="scan" style="font-size:12px;margin-top:2px"></div>
<div class="err" id="err"></div>
<div class="tiles" id="tiles"></div>
<div class="wrap" style="padding:8px 12px"><div class="mut key">Cumulative since go-live, a point every 5 min:
<b style="color:var(--est)">&#9644; est rewards</b> &nbsp;<b style="color:var(--trade)">&#9644; trading P&amp;L</b> &nbsp;<b>&#9644; net</b></div>
<svg id="chart" viewBox="0 0 800 220" style="width:100%;height:220px;display:block"></svg></div>
<div class="wrap" style="padding:8px 12px;margin-top:8px"><div class="mut key">Fills since go-live, cumulative (contracts):
<b>&#9644; total</b> &nbsp;<b style="color:var(--up)">&#9644; YES bought (our bids)</b> &nbsp;<b style="color:var(--dn)">&#9644; YES sold (our asks)</b></div>
<svg id="fcum" viewBox="0 0 800 180" style="width:100%;height:180px;display:block"></svg></div>
<div class="wrap" style="padding:8px 12px;margin-top:8px"><div class="mut key">Fills per 5 minutes (contracts):
<b style="color:var(--up)">&#9632; YES bought</b> &nbsp;<b style="color:var(--dn)">&#9632; YES sold</b></div>
<svg id="fbar" viewBox="0 0 800 160" style="width:100%;height:160px;display:block"></svg></div>
<h2>Dead books vs live books</h2>
<div class="wrap"><table><thead><tr><th>Kind</th><th>Listed</th><th>Quoting</th><th>Est rewards</th><th>Trading P&amp;L</th><th>realized / open</th><th>Net</th><th>Est $/day now</th><th>Fills</th><th>Mkts with a position</th></tr></thead><tbody id="kinds"></tbody></table></div>
<div class="wrap" style="padding:8px 12px;margin-top:8px"><div class="mut key">Net (est rewards + trading) by kind, cumulative:
<b style="color:var(--dead)">&#9644; dead books</b> &nbsp;<b style="color:var(--live)">&#9644; live books</b> &nbsp;<span class="mut">(dashed = trading P&amp;L only)</span></div>
<svg id="kchart" viewBox="0 0 800 180" style="width:100%;height:180px;display:block"></svg></div>
<h2>By source <span class="mut" style="font-weight:400;font-size:12px">(11/04 list = the hand-built list; scan = added by pvd_scan.py every 15 min: new programs, relit periods, extended stops)</span></h2>
<div class="wrap"><table><thead><tr><th>Source</th><th>Entries</th><th>Active</th><th>Quoting</th><th>Est rewards</th><th>Trading P&amp;L</th><th>Net</th><th>Est $/day now</th><th>Fills</th></tr></thead><tbody id="src"></tbody></table></div>
<h2>By series</h2>
<div class="wrap"><table><thead><tr><th>Series</th><th>Listed</th><th>Quoting</th><th>Est rewards</th><th>Trading P&amp;L</th><th>Net</th><th>Est $/day now</th><th>Fills</th></tr></thead><tbody id="ser"></tbody></table></div>
<h2>Markets <span class="mut" style="font-weight:400;font-size:12px">(click a header to sort; highlighted = holding a position from these quotes; faded = no order resting or past its stop. Banked = est rewards this program period, vs Kalshi's $1 floor)</span></h2>
<div class="wrap"><table><thead><tr id="mh"></tr></thead><tbody id="rows"></tbody></table></div>
<h2>Recent fills</h2>
<div class="wrap"><table><thead><tr><th>Time (UTC)</th><th>Market</th><th>Side</th><th>Qty</th><th>Price</th></tr></thead><tbody id="fills"></tbody></table></div>
<script>
const $=id=>document.getElementById(id);
const money=v=>(v<0?'-$':'$')+Math.abs(v).toFixed(2);
const cls=v=>v>0.005?'up':v<-0.005?'dn':'';
const COLS=[['ticker','Market'],['kind','Kind'],['src','Source'],['stop','Stop (ET)'],['size','Size'],['bid','Book bid'],['ask','Book ask'],['last','Last'],['our_bid','Our bid'],['our_ask','Our ask'],
 ['limits','Limits'],['pos','Pos'],['trade','Trading P&L'],['est','Est rewards'],['net','Net'],['period_est','Banked (period)'],['proj_period','Proj. by stop'],['rate','Est $/day'],['share','Share'],['n_fills','Fills']];
const etS=s=>s?new Date(s).toLocaleString('en-US',{timeZone:'America/New_York',month:'numeric',day:'numeric',hour:'numeric',minute:'2-digit'}):'-';
const bar=v=>v>=1.3?'up':v>=1.0?'':'dn';
let sortK='trade',sortDir=1,D=null;
function sortBy(k){if(sortK===k)sortDir=-sortDir;else{sortK=k;sortDir=(k==='ticker'?1:(k==='trade'?1:-1))}render()}
$('mh').innerHTML=COLS.map(([k,n])=>`<th onclick="sortBy('${k}')">${n}</th>`).join('');
async function tick(){
  try{const r=await fetch('/data',{cache:'no-store'});const j=await r.json();
    $('err').textContent=j.err||'';if(j.data){D=j.data;D._next=j.next;render()}}catch(e){$('err').textContent='page: '+e}
}
function render(){
  const d=D,t=d.totals;
  const nxt=d._next?Math.max(0,Math.round(d._next-Date.now()/1000)):null;
  const st=d.stale||{};$('err').textContent=(st.markets||st.books)?`Kalshi rate limit on this read: ${st.markets||0} market and ${st.books||0} book reads reused from the previous read`:($('err').textContent||'');
  $('sub').textContent='updated '+new Date(d.now).toLocaleTimeString('en-US',{timeZone:'America/New_York'})+' ET'+
    (nxt!=null?' | next read in '+Math.ceil(nxt/60)+' min':'')+' | live since '+new Date(d.go_live).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'})+' ET | '+
    (d.end?'next stop '+etS(d.end)+' ET (in '+d.hrs_left.toFixed(1)+'h): '+(d.stop_mix||[]).map(([s,n])=>n+' to '+etS(s)).join(', '):'no listed market is active');
  const sc=d.scan;
  $('scan').innerHTML=sc?`Overnight scanner (pvd_scan.py, every 15 min): last run ${etS(sc.at)} ET${sc.write?'':' (dry run)'} &middot; ${sc.candidates??0} candidate(s) `+
    `(extended ${sc.extend_candidates??0}, relit ${sc.relit_candidates??0}, new ${sc.new_candidates??0}) &middot; ${sc.past_vote??0} past their vote &middot; `+
    `${sc.scored??0} clear $${(sc.bar||1.3).toFixed(2)} &middot; <b>added ${sc.taken_n??0}</b>${sc.taken_n?` (~${money(sc.taken_usd_day||0)}/day, ${money(sc.taken_collateral||0)} at risk if filled)`:''}`+
    `${sc.over_budget?` &middot; ${sc.over_budget} over the ${money(sc.budget)} budget`:''} &middot; ${sc.secs}s`
    :'Overnight scanner: no run yet';
  $('tiles').innerHTML=[
    ['big','Net (est rewards + trading)',`<span class="${cls(t.net)}">${money(t.net)}</span>`,''],
    ['big','Est rewards so far',`<span style="color:var(--est)">${money(t.est)}</span>`,`projected ${money(t.proj_est)} by the end at today's rate`],
    ['big','Trading P&L',`<span class="${cls(t.trade)}">${money(t.trade)}</span>`,`realized ${money(t.real)} &middot; open ${money(t.open)} (at last trade)`],
    ['','Est reward rate now',money(t.rate)+'/day',`raw model; Kalshi has paid ~1.15x`],
    ['','Markets quoting',`${t.quoted} / ${t.listed}`,`${t.two_sided} scoring both sides &middot; ${t.rest_ct.toLocaleString()} contracts resting${t.ended?` &middot; ${t.ended} past their stop`:''}`],
    ['','This period vs $1',`<span class="up">${t.period_ok}</span> / <span class="dn">${t.period_short}</span>`,`active markets projected over / under $1 by their stop`],
    ['','Fills since go-live',`${t.fills}`,`${Math.round(t.fill_ct).toLocaleString()} contracts &middot; open cost ${money(t.cost)}`]
  ].map(([c,k,v,s])=>`<div class="tile ${c}"><span class="mut">${k}</span><b>${v}</b><small>${s}</small></div>`).join('');
  $('kinds').innerHTML=(d.by_kind||[]).map(k=>`<tr><td><span class="tag ${k.kind}">${k.kind} books</span></td><td>${k.n}</td><td>${k.quoted}</td>
    <td style="color:var(--est)">${money(k.est)}</td><td class="${cls(k.trade)}"><b>${money(k.trade)}</b></td><td class="mut">${money(k.real)} / ${money(k.open)}</td>
    <td class="${cls(k.net)}"><b>${money(k.net)}</b></td><td>${money(k.rate)}</td><td>${k.fills}</td><td>${k.pos_mkts}</td></tr>`).join('');
  kchart(d.series);
  $('src').innerHTML=(d.by_src||[]).map(b=>`<tr><td>${b.src}</td><td>${b.n}</td><td>${b.active}</td><td>${b.quoted}</td>
    <td style="color:var(--est)">${money(b.est)}</td><td class="${cls(b.trade)}">${money(b.trade)}</td><td class="${cls(b.net)}"><b>${money(b.net)}</b></td>
    <td>${money(b.rate)}</td><td>${b.fills}</td></tr>`).join('');
  $('ser').innerHTML=d.by_series.map(b=>`<tr><td>${b.series}</td><td>${b.n}</td><td>${b.quoted}</td>
    <td style="color:var(--est)">${money(b.est)}</td><td class="${cls(b.trade)}">${money(b.trade)}</td><td class="${cls(b.net)}"><b>${money(b.net)}</b></td>
    <td>${money(b.rate)}</td><td>${b.fills}</td></tr>`).join('');
  const rows=[...d.rows].sort((a,b)=>{const x=a[sortK],y=b[sortK];
    const ax=Array.isArray(x)?x[0]:x,by=Array.isArray(y)?y[0]:y;
    if(ax==null)return 1;if(by==null)return -1;return (ax>by?1:ax<by?-1:0)*sortDir});
  const v=x=>x??'<span class="mut">-</span>';
  $('rows').innerHTML=rows.map(r=>`<tr class="${r.pos?'pos':''} ${(r.rest_ct&&r.active)?'':'off'}"><td title="${r.sub}">${r.ticker}<div class="mut" style="font-size:11px">${r.sub}</div></td>
    <td><span class="tag ${r.kind}">${r.kind}</span></td><td class="mut">${r.src}</td><td class="${r.active?'':'dn'}">${r.active?etS(r.stop):'ended '+etS(r.stop)}</td>
    <td>${r.bsize===r.asize?r.bsize:r.bsize+' / '+r.asize}</td>
    <td>${v(r.bid)}</td><td>${v(r.ask)}</td><td>${v(r.last)}</td><td>${v(r.our_bid)}</td><td>${v(r.our_ask)}</td>
    <td class="mut">&le;${r.limits[0]} / &ge;${r.limits[1]}</td><td class="${cls(r.pos)}">${r.pos||''}</td>
    <td class="${cls(r.trade)}">${money(r.trade)}</td><td style="color:var(--est)">${money(r.est)}</td><td class="${cls(r.net)}"><b>${money(r.net)}</b></td>
    <td class="${bar(r.period_est)}">${money(r.period_est)}</td><td class="${r.active?bar(r.proj_period):'mut'}">${money(r.proj_period)}</td>
    <td>${money(r.rate)}</td><td>${(r.share*100).toFixed(1)}%</td><td>${r.n_fills||''}</td></tr>`).join('');
  $('fills').innerHTML=d.recent.map(f=>`<tr><td>${f.ts}</td><td>${f.ticker}</td><td class="${f.side==='BUY YES'?'up':'dn'}">${f.side}</td><td>${f.n}</td><td>${f.px}c</td></tr>`).join('')||'<tr><td colspan="5" class="mut">none yet</td></tr>';
  chart(d.series);
  fillCharts(d.fill_series);
}
function chart(s){
  const svg=$('chart'),W=Math.max(320,Math.round(svg.clientWidth||800)),H=220,L=56,R=70,T=10,B=24;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  if(!s||!s.length){svg.innerHTML='';return}
  const pts=[[Date.parse(D.go_live)/1000,0,0,0],...s];
  const x0=pts[0][0],x1=Math.max(pts[pts.length-1][0],x0+600);
  let lo=0,hi=0;for(const p of pts)for(const v of p.slice(1)){lo=Math.min(lo,v);hi=Math.max(hi,v)}
  if(hi-lo<1){hi+=0.5;lo-=0.5}
  const X=x=>L+(x-x0)/(x1-x0)*(W-L-R),Y=y=>T+(hi-y)/(hi-lo)*(H-T-B);
  const et=t=>new Date(t*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'});
  let g='';for(const v of [lo,0,hi])g+=`<line x1="${L}" x2="${W-R}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v===0?'var(--mut)':'var(--line)'}"/><text x="${L-6}" y="${Y(v)+4}" text-anchor="end" font-size="11" fill="var(--mut)">${money(v)}</text>`;
  for(const t of [x0,(x0+x1)/2,x1])g+=`<text x="${X(t)}" y="${H-6}" text-anchor="middle" font-size="11" fill="var(--mut)">${et(t)}</text>`;
  const ser=[[1,'var(--est)',2],[2,'var(--trade)',2],[3,'var(--fg)',2.5]];
  const ends=[];
  for(const [i,col,w] of ser){
    g+=`<polyline points="${pts.map(p=>X(p[0]).toFixed(1)+','+Y(p[i]).toFixed(1)).join(' ')}" fill="none" stroke="${col}" stroke-width="${w}" vector-effect="non-scaling-stroke"/>`;
    const p=pts[pts.length-1];ends.push([Y(p[i]),col,p[i],X(p[0])])}
  ends.sort((a,b)=>a[0]-b[0]);for(let i=1;i<ends.length;i++)if(ends[i][0]-ends[i-1][0]<13)ends[i][0]=ends[i-1][0]+13;
  for(const [y,col,val,x] of ends)g+=`<text x="${x+6}" y="${y+4}" font-size="12" font-weight="600" fill="${col}">${money(val)}</text>`;
  svg.innerHTML=g;
}
function fillCharts(fs){
  const et=t=>new Date(t*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'});
  const num=v=>Math.round(v).toLocaleString();
  for(const id of ['fcum','fbar']){const svg=$(id);svg.innerHTML='';}
  if(!fs||!fs.bid||!fs.bid.length)return;
  const n=fs.bid.length,ts=i=>fs.t0+fs.step*(i+1);
  // cumulative lines
  {const svg=$('fcum'),W=Math.max(320,Math.round(svg.clientWidth||800)),H=180,L=56,R=70,T=10,B=24;svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
   let cb=0,ca=0;const pts=[[fs.t0,0,0,0]];
   for(let i=0;i<n;i++){cb+=fs.bid[i];ca+=fs.ask[i];pts.push([ts(i),cb+ca,cb,ca])}
   const x0=fs.t0,x1=Math.max(pts[pts.length-1][0],x0+600),hi=Math.max(1,...pts.map(p=>p[1]));
   const X=x=>L+(x-x0)/(x1-x0)*(W-L-R),Y=y=>T+(hi-y)/hi*(H-T-B);
   let g='';for(const y of [0,hi/2,hi])g+=`<line x1="${L}" x2="${W-R}" y1="${Y(y)}" y2="${Y(y)}" stroke="var(--line)"/><text x="${L-6}" y="${Y(y)+4}" text-anchor="end" font-size="11" fill="var(--mut)">${num(y)}</text>`;
   for(const t of [x0,(x0+x1)/2,x1])g+=`<text x="${X(t)}" y="${H-6}" text-anchor="middle" font-size="11" fill="var(--mut)">${et(t)}</text>`;
   const ends=[];
   for(const [i,col,w] of [[1,'var(--fg)',2.5],[2,'var(--up)',1.8],[3,'var(--dn)',1.8]]){
     g+=`<polyline points="${pts.map(p=>X(p[0]).toFixed(1)+','+Y(p[i]).toFixed(1)).join(' ')}" fill="none" stroke="${col}" stroke-width="${w}" vector-effect="non-scaling-stroke"/>`;
     const p=pts[pts.length-1];ends.push([Y(p[i]),col,p[i],X(p[0])])}
   ends.sort((a,b)=>a[0]-b[0]);for(let i=1;i<ends.length;i++)if(ends[i][0]-ends[i-1][0]<13)ends[i][0]=ends[i-1][0]+13;
   for(const [y,col,v,x] of ends)g+=`<text x="${x+6}" y="${y+4}" font-size="12" font-weight="600" fill="${col}">${num(v)}</text>`;
   svg.innerHTML=g;}
  // per-5-min bars, bought above the axis, sold below
  {const svg=$('fbar'),W=Math.max(320,Math.round(svg.clientWidth||800)),H=160,L=56,R=12,T=8,B=22;svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
   const mb=Math.max(1,...fs.bid),ma=Math.max(1,...fs.ask),mid=T+(H-T-B)*mb/(mb+ma);
   const pw=(W-L-R)/n,bw=Math.max(1,pw*0.8),sc=(H-T-B)/(mb+ma);
   let g=`<line x1="${L}" x2="${W-R}" y1="${mid}" y2="${mid}" stroke="var(--mut)"/>`+
     `<text x="${L-6}" y="${T+8}" text-anchor="end" font-size="11" fill="var(--mut)">${num(mb)}</text>`+
     `<text x="${L-6}" y="${H-B}" text-anchor="end" font-size="11" fill="var(--mut)">${num(ma)}</text>`;
   for(let i=0;i<n;i++){const x=L+pw*i+(pw-bw)/2,lab=`${et(ts(i)-fs.step)}-${et(ts(i))}`;
     if(fs.bid[i])g+=`<rect x="${x.toFixed(1)}" y="${(mid-fs.bid[i]*sc).toFixed(1)}" width="${bw.toFixed(1)}" height="${(fs.bid[i]*sc).toFixed(1)}" fill="var(--up)"><title>${lab}: bought ${num(fs.bid[i])}</title></rect>`;
     if(fs.ask[i])g+=`<rect x="${x.toFixed(1)}" y="${mid.toFixed(1)}" width="${bw.toFixed(1)}" height="${(fs.ask[i]*sc).toFixed(1)}" fill="var(--dn)"><title>${lab}: sold ${num(fs.ask[i])}</title></rect>`}
   for(const i of [0,Math.floor((n-1)/2),n-1])g+=`<text x="${L+pw*i+pw/2}" y="${H-6}" text-anchor="middle" font-size="11" fill="var(--mut)">${et(ts(i))}</text>`;
   svg.innerHTML=g;}
}
function kchart(s){
  const svg=$('kchart'),W=Math.max(320,Math.round(svg.clientWidth||800)),H=180,L=56,R=70,T=10,B=24;
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  const pts=(s||[]).filter(p=>p.length>=8);if(!pts.length){svg.innerHTML='<text x="10" y="20" font-size="12" fill="var(--mut)">split starts at the next 5-min read</text>';return}
  const v=p=>[p[4]+p[5],p[5],p[6]+p[7],p[7]];
  const x0=pts[0][0],x1=Math.max(pts[pts.length-1][0],x0+600);
  let lo=0,hi=0;for(const p of pts)for(const y of v(p)){lo=Math.min(lo,y);hi=Math.max(hi,y)}
  if(hi-lo<1){hi+=0.5;lo-=0.5}
  const X=x=>L+(x-x0)/(x1-x0)*(W-L-R),Y=y=>T+(hi-y)/(hi-lo)*(H-T-B);
  const et=t=>new Date(t*1000).toLocaleTimeString('en-US',{timeZone:'America/New_York',hour:'numeric',minute:'2-digit'});
  let g='';for(const y of [lo,0,hi])g+=`<line x1="${L}" x2="${W-R}" y1="${Y(y)}" y2="${Y(y)}" stroke="${y===0?'var(--mut)':'var(--line)'}"/><text x="${L-6}" y="${Y(y)+4}" text-anchor="end" font-size="11" fill="var(--mut)">${money(y)}</text>`;
  for(const t of [x0,(x0+x1)/2,x1])g+=`<text x="${X(t)}" y="${H-6}" text-anchor="middle" font-size="11" fill="var(--mut)">${et(t)}</text>`;
  const ends=[];
  for(const [i,col,dash] of [[0,'var(--dead)',''],[1,'var(--dead)','4 3'],[2,'var(--live)',''],[3,'var(--live)','4 3']]){
    g+=`<polyline points="${pts.map(p=>X(p[0]).toFixed(1)+','+Y(v(p)[i]).toFixed(1)).join(' ')}" fill="none" stroke="${col}" stroke-width="${dash?1.5:2.5}" stroke-dasharray="${dash}" vector-effect="non-scaling-stroke"/>`;
    if(!dash){const p=pts[pts.length-1];ends.push([Y(v(p)[i]),col,v(p)[i],X(p[0])])}}
  ends.sort((a,b)=>a[0]-b[0]);for(let i=1;i<ends.length;i++)if(ends[i][0]-ends[i-1][0]<13)ends[i][0]=ends[i-1][0]+13;
  for(const [y,col,val,x] of ends)g+=`<text x="${x+6}" y="${y+4}" font-size="12" font-weight="600" fill="${col}">${money(val)}</text>`;
  svg.innerHTML=g;
}
tick();setInterval(tick,30000);addEventListener('resize',()=>{if(D){chart(D.series);kchart(D.series);fillCharts(D.fill_series)}});
</script></body></html>"""


class Handler(__import__("http.server").server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/data"):
            with _lock:
                body = json.dumps({"data": _state["data"], "err": _state["err"],
                                   "next": _state["next"]}).encode()
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
    from http.server import ThreadingHTTPServer
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8771)
    a = ap.parse_args()
    # no SO_REUSEADDR: on Windows it lets a second server bind a port another
    # page already serves (8765/8766 run the ENS page), and the browser then
    # reaches the wrong one -- fail loudly instead
    ThreadingHTTPServer.allow_reuse_address = False
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    threading.Thread(target=poller, daemon=True).start()
    print(f"imm_pvd_watch: http://127.0.0.1:{a.port}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
