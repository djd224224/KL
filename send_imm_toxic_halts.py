#!/usr/bin/env python3
r"""send_imm_toxic_halts.py -- the daily "what did the toxic-flow halt stand
down yesterday" email (Jack 2026-09-29: "and give me a daily morning email on
what was halted in the prior day").

WHAT IT REPORTS, for the prior ET calendar day:
  * PROFIT (Jack 2026-10-19: "in daily toxic halt emil tell me profit"), up
    top and in the subject: what the halts made or cost -- the fills they
    blocked against the reward they forwent -- and what the pick-offs
    themselves lost, plus the last 7 days (how: "PROFIT" below);
  * every EVENT halt (pick-offs on TOXIC_EVENT_MARKETS different markets of
    one event inside TOXIC_EVENT_WINDOW_SECS -> the whole event out for
    TOXIC_EVENT_HALT_SECS): which markets were picked off on which side, how
    many markets the halt took down, and the reward it forwent;
  * every SIDE halt (TOXIC_PICKOFFS pick-offs on one side of a market inside
    TOXIC_WINDOW_SECS -> that side out for TOXIC_HALT_SECS): the pick-offs
    that caused it (fill price -> mark 5 minutes later), the forgone reward,
    and what the price did DURING the halt -- still moving against the side
    means the halt kept the bot out of it; moving back means it cost rent;
  * the most picked-off markets, including pick-offs that never reached a
    halt.
The rule itself lives in incentive_mm.py (TOXIC_* knobs, _toxic_confirm).

PROFIT. A halt's net = -(what the fills it blocked would have made) - (the
reward it forwent); + means the halt paid:
  quotes   what the bot would have had up during the halt: the quotes the
           halt took off (the bot's cancels on the halted side, or on every
           market of a halted event, within QUOTE_WINDOW_SECS of it firing).
           Where nothing was up -- another stand-down had them off, a sweep
           breaker say -- the quotes it put back within QUOTE_WINDOW_SECS of
           the halt ending. Neither = that side was not going to quote
           (quiet hours, a position cap): the halt changed nothing there.
  blocked  every public trade during the halt at or through one of those
           prices (a taker sale at/under a bid, a taker buy at/over an ask),
           capped at the quote's size, best price first, marked to the price
           now -- the settlement once settled -- as the lone-ref guard email
           does. A trade AT our price counts in full (the queue may have
           been ahead of us), so this is the most the halt could have
           saved or cost.
  forgone  the bot's own estimate (what each halt recorded), on the share of
           the halted markets that had quotes as above.
The pick-offs' own P&L, per market in the most-picked-off table: each fill's
contracts (from the fills sink) times the move to the 5-minute check, and to
the price now. No day total (Jack 2026-10-22: "Remove the -236$ part of
toxic email, it's misleading"): pick-offs are the fills chosen for having
gone against us, so their sum reads as a loss on any day.

SOURCES: the bot's own toxic_halts_<UTC date>.jsonl sink (kind = pickoff /
side_halt / event_halt, written as each one happens), marks_<UTC
date>.jsonl (the 5-minute mark of every open position) for the price during
a halt, fills_<UTC date>.jsonl (each pick-off's contracts), orders_<UTC
date>.jsonl (the quotes each halt pulled or put back) and Kalshi's PUBLIC
market data (GET /markets, /markets/trades: no auth, paced). READ-ONLY: no
account calls, no orders; the files written are the daily sent-marker and
toxic_halts_profit.json (each day's profit, for the 7-day line). Imports
imm_quote_gaps first, like the other morning emails, so the launcher env and
the alert credentials apply. Runs at idle priority: the bot shares the PC.

USAGE:
  python send_imm_toxic_halts.py                   # the scheduled daily run
  python send_imm_toxic_halts.py --dry             # build + print, send nothing
  python send_imm_toxic_halts.py --test            # send now, ignore the marker
  python send_imm_toxic_halts.py --day 2026-09-28  # a specific ET day
Scheduled daily as "KL imm toxic-halts" (register_imm_toxic_halts.ps1).
Sends every morning, including "nothing halted", so a silent day is
distinguishable from a broken task. Only the scheduled run writes
toxic_halts_profit.json; every run fills the 7-day line's missing days
from the sinks. All console output is ASCII (the task console is cp1252).
"""
import argparse
import bisect
import ctypes
import glob
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape as _esc

# imm_quote_gaps FIRST: it mirrors the launcher's $ProbeEnv and the alert
# credential fallback into the environment before incentive_mm reads config.
import imm_quote_gaps as _gaps  # noqa: F401  (import side effects)
import incentive_mm as imm
from incentive_mm import log

MAX_ROWS = int(os.environ.get("IMM_TOXIC_EMAIL_MAX_ROWS", "60"))
TD = 'padding:4px 9px;border:1px solid #ddd;text-align:right;'
TDL = 'padding:4px 9px;border:1px solid #ddd;text-align:left;'
KALSHI = os.environ.get("IMM_TOXIC_EMAIL_KALSHI",
                        "https://api.elections.kalshi.com/trade-api/v2")
API_GAP_SECS = 0.5             # between public reads (the bot shares the IP)
QUOTE_WINDOW_SECS = 180.0      # a halt's cancels, and the put-backs as it ends,
                               # land within a cycle or two
TRAIL_DAYS = 7


def low_priority() -> None:
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.SetPriorityClass(k32.GetCurrentProcess(), 0x40)      # IDLE_PRIORITY_CLASS
    except Exception:
        pass


def store_path() -> str:
    return os.path.join(imm.STATUS_DIR, "toxic_halts_profit.json")


def day_window(day) -> tuple:
    """[00:00 ET day, 00:00 ET next day) as UTC datetimes."""
    start = imm.ET.localize(datetime(day.year, day.month, day.day))
    end = imm.ET.localize(datetime(day.year, day.month, day.day) + timedelta(days=1))
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _utc_dates(start: datetime, end: datetime) -> list:
    d, out = start.date(), []
    while d <= (end - timedelta(seconds=1)).date():
        out.append(d)
        d += timedelta(days=1)
    return out


def load_records(start: datetime, end: datetime, status_dir=None) -> tuple:
    """(records inside [start, end), whether any sink file existed)."""
    status_dir = status_dir or imm.STATUS_DIR
    lo, hi = start.timestamp(), end.timestamp()
    rows, seen_file = [], False
    for d in _utc_dates(start, end + timedelta(days=1)):
        path = os.path.join(status_dir, f"toxic_halts_{d:%Y-%m-%d}.jsonl")
        if not os.path.exists(path):
            continue
        seen_file = True
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if lo <= float(r.get("ts") or 0) < hi:
                    rows.append(r)
    rows.sort(key=lambda r: r["ts"])
    return rows, seen_file


def load_marks(tickers: set, start: datetime, end: datetime,
               status_dir=None) -> dict:
    """ticker -> sorted [(ts, mark_cents)] from the marks sink (open
    positions only, every ~5 min), over the window plus a day of spill."""
    status_dir = status_dir or imm.STATUS_DIR
    out = defaultdict(list)
    if not tickers:
        return out
    for d in _utc_dates(start, end + timedelta(days=1)):
        path = os.path.join(status_dir, f"marks_{d:%Y-%m-%d}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                i = line.find('"ticker": "')
                if i < 0:
                    continue
                t = line[i + 11:line.find('"', i + 11)]
                if t not in tickers:
                    continue
                try:
                    r = json.loads(line)
                    mk = r.get("mark_cents")
                    if mk is None:
                        continue
                    ts = datetime.fromisoformat(r["ts"]).timestamp()
                except (ValueError, KeyError):
                    continue
                out[t].append((ts, float(mk)))
    for t in out:
        out[t].sort()
    return out


def mark_at(marks: dict, t: str, ts: float, slack: float = 900.0):
    """First mark at/after ts within `slack` seconds, else None."""
    arr = marks.get(t) or []
    k = bisect.bisect_left(arr, (ts, -1e9))
    if k < len(arr) and arr[k][0] - ts <= slack:
        return arr[k][1]
    return None


# ---- PROFIT: the sinks ------------------------------------------------------------

def _sign(side: str) -> float:
    """+1 for a bid (we bought YES), -1 for an ask (we sold YES)."""
    return 1.0 if side == "bid" else -1.0


def pickoff_sizes(picks: list, start: datetime, end: datetime, status_dir=None) -> None:
    """Set each pick-off's "count" (contracts; None when no fill matches) and
    "usd5" ($ at the 5-minute check) from the fills sink. The bot queues
    every maker fill for its check with that fill's ts and YES price, so the
    match is one to one."""
    status_dir = status_dir or imm.STATUS_DIR
    if not picks:
        return
    want = {(p["ticker"], p["side"]) for p in picks}
    pool = defaultdict(list)
    for d in _utc_dates(start - timedelta(hours=2), end):
        path = os.path.join(status_dir, f"fills_{d:%Y-%m-%d}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    f = json.loads(line)
                except ValueError:
                    continue
                key = (f.get("ticker"), f.get("our_book_side"))
                if key in want and not f.get("is_taker"):
                    pool[key].append(f)
    used = set()
    for p in picks:
        p["count"] = p["usd5"] = None
        for f in pool.get((p["ticker"], p["side"]), ()):
            if (id(f) not in used
                    and abs(float(f.get("ts") or 0) - float(p.get("fill_ts") or 0)) <= 2.0
                    and abs(float(f.get("yes_price_cents") or -1) - float(p["fill_px"])) < 0.01):
                used.add(id(f))
                p["count"] = float(f.get("count") or 0.0)
                p["usd5"] = (_sign(p["side"]) * (float(p["mark"]) - float(p["fill_px"]))
                             * p["count"] / 100.0)
                break


def halt_quotes(halts: list, status_dir=None) -> None:
    """Set each halt's "quotes": [(ticker, side, price c, contracts, how)] --
    what the bot would have had up during it. Per halted market and side:
    the quotes the halt pulled ("pulled": the bot's cancels within
    QUOTE_WINDOW_SECS after it fired; the next cycle wants nothing there, so
    the requote cancels them), else the ones it put back within
    QUOTE_WINDOW_SECS of the halt ending ("put back": the first cycle's).
    One pass over the orders sink."""
    status_dir = status_dir or imm.STATUS_DIR
    for h in halts:
        h["quotes"] = []
    if not halts:
        return
    tick = {h["ticker"] for h in halts if h["kind"] == "side_halt"}
    evs = {h["event"] for h in halts if h["kind"] == "event_halt"}
    lo = min(float(h["ts"]) for h in halts)
    hi = max(float(h["until"]) for h in halts) + QUOTE_WINDOW_SECS
    rows = []
    for d in _utc_dates(datetime.fromtimestamp(lo, timezone.utc),
                        datetime.fromtimestamp(hi, timezone.utc)):
        path = os.path.join(status_dir, f"orders_{d:%Y-%m-%d}.jsonl")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if '"kind": "cancel"' not in line and '"kind": "place"' not in line:
                    continue
                i = line.find('"ticker": "')
                t = line[i + 11:line.find('"', i + 11)] if i >= 0 else ""
                j = line.find('"event_ticker": "')
                e = line[j + 17:line.find('"', j + 17)] if j >= 0 else ""
                if t not in tick and e not in evs:
                    continue
                try:
                    r = json.loads(line)
                    ts = datetime.fromisoformat(r["ts"]).timestamp()
                except (ValueError, KeyError):
                    continue
                if lo <= ts <= hi and not r.get("is_pad"):
                    rows.append((ts, r))
    rows.sort(key=lambda x: x[0])
    taken = set()                      # an order is pulled by one halt only
    for h in sorted(halts, key=lambda x: x["ts"]):
        t0, t1 = float(h["ts"]), float(h["until"])

        def mine(r) -> bool:
            if h["kind"] == "side_halt":
                return r["ticker"] == h["ticker"] and r["book_side"] == h["side"]
            return r.get("event_ticker") == h["event"]

        pulled, back, back_cycle = defaultdict(list), defaultdict(list), {}
        for ts, r in rows:
            key = (r["ticker"], r["book_side"])
            q = (r["ticker"], r["book_side"], float(r["price_cents"]),
                 float(r.get("count") or 0.0))
            if (r["kind"] == "cancel" and t0 <= ts <= t0 + QUOTE_WINDOW_SECS
                    and r["order_id"] not in taken and mine(r)):
                taken.add(r["order_id"])
                pulled[key].append(q + ("pulled",))
            elif r["kind"] == "place" and t1 <= ts <= t1 + QUOTE_WINDOW_SECS and mine(r):
                cyc = back_cycle.setdefault(key, r.get("cycle_ts") or ts)
                if (r.get("cycle_ts") or ts) == cyc:
                    back[key].append(q + ("put back",))
        for key in sorted(set(pulled) | set(back)):
            h["quotes"] += pulled.get(key) or back[key]


# ---- PROFIT: Kalshi's public market data -------------------------------------------

_last_call = [0.0]


def _get(path: str, params: dict) -> dict:
    """One paced public GET (no auth); 429s back off and retry."""
    wait = API_GAP_SECS - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    url = f"{KALSHI}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    for i in range(6):
        _last_call[0] = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "imm-toxic-halts"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"429s on {path}")


def market_value(m: dict):
    """YES cents a market is worth now: its settlement once settled (a
    settled book reads bid 0 / ask 100, which is no price), else the mid,
    the one side there is when the other is empty, or the last trade; None
    with no price at all."""
    sv = m.get("settlement_value_dollars")
    if sv not in (None, ""):
        return float(sv) * 100.0
    if m.get("result") in ("yes", "no"):
        return 100.0 if m["result"] == "yes" else 0.0
    b = float(m.get("yes_bid_dollars") or 0) * 100.0
    a = float(m.get("yes_ask_dollars") or 0) * 100.0
    if b > 0 and 0 < a < 100:
        return (b + a) / 2.0
    if b > 0:
        return b
    if 0 < a < 100:
        return a
    return float(m.get("last_price_dollars") or 0) * 100.0 or None


def trades_between(ticker: str, a_ts: float, b_ts: float) -> list:
    """Every public trade on `ticker` from a_ts to b_ts."""
    out, cur = [], ""
    while True:
        params = {"ticker": ticker, "min_ts": int(a_ts), "max_ts": int(b_ts) + 1,
                  "limit": 1000}
        if cur:
            params["cursor"] = cur
        r = _get("/markets/trades", params)
        out += r.get("trades") or []
        cur = r.get("cursor") or ""
        if not cur:
            return out


def blocked_fills(quotes: list, trades: list) -> list:
    """(quote, contracts) for each public trade through a quote while its
    halt ran: a taker sale at or under a bid, a taker buy at or over an ask
    (YES cents). One market's quotes and trades. Each quote fills at most its
    size, the best price first, as the trades come."""
    left = {id(q): q["size"] for q in quotes}
    out = []
    for tr in sorted(trades, key=lambda x: x["created_time"]):
        side = {"no": "bid", "yes": "ask"}.get(tr.get("taker_side"))
        if side is None:
            continue
        tts = datetime.fromisoformat(tr["created_time"].replace("Z", "+00:00")).timestamp()
        yes = float(tr["yes_price_dollars"]) * 100.0
        n_tr = float(tr.get("count_fp") or tr.get("count") or 0.0)
        hit = [q for q in quotes if q["side"] == side and q["start"] <= tts < q["end"]
               and left[id(q)] > 0
               and (q["px"] >= yes - 1e-9 if side == "bid" else q["px"] <= yes + 1e-9)]
        hit.sort(key=lambda q: -q["px"] if side == "bid" else q["px"])
        for q in hit:
            if n_tr <= 0:
                break
            n = min(n_tr, left[id(q)])
            left[id(q)] -= n
            n_tr -= n
            out.append((q, n))
    return out


def price_profit(picks: list, halts: list) -> bool:
    """The Kalshi reads: each halted market's trades while halted and each
    market's price now. Sets each halt's "blocked" ($ its blocked fills
    would have made; None when unread or unpriced) and "blocked_ct", and
    each matched pick-off's "now" ($ marked to now). True when every read
    worked; a failed read leaves that market n/a, never the email."""
    values, failed = {}, set()

    def value(t):
        if t not in values and t not in failed:
            try:
                values[t] = market_value(_get(f"/markets/{t}", {}).get("market") or {})
            except Exception as e:                  # one market never costs the email
                log(f"toxic-halts: price read failed for {t}: {e!r}")
                failed.add(t)
        return values.get(t)

    by_t = defaultdict(list)
    for h in halts:
        h["blocked"], h["blocked_ct"] = 0.0, 0.0
        for t, side, px, n, _how in h.get("quotes") or []:
            by_t[t].append(dict(halt=h, side=side, px=px, size=n,
                                start=float(h["ts"]), end=float(h["until"])))
    for t, qs in sorted(by_t.items()):
        try:
            got = blocked_fills(qs, trades_between(t, min(q["start"] for q in qs),
                                                   max(q["end"] for q in qs)))
        except Exception as e:
            log(f"toxic-halts: trades read failed for {t}: {e!r}")
            failed.add(t)
            for q in qs:
                q["halt"]["blocked"] = None
            continue
        v = value(t) if got else None
        for q, n in got:
            h = q["halt"]
            if h["blocked"] is None:
                continue
            if v is None:
                h["blocked"] = None
                continue
            h["blocked"] += _sign(q["side"]) * (v - q["px"]) * n / 100.0
            h["blocked_ct"] += n
    for p in picks:
        p["now"] = None
        if p.get("count") is not None:
            v = value(p["ticker"])
            if v is not None:
                p["now"] = _sign(p["side"]) * (v - float(p["fill_px"])) * p["count"] / 100.0
    return not failed


# ---- the report ------------------------------------------------------------------

def _et(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone(imm.ET).strftime("%H:%M")


def _short(t: str, ev: str) -> str:
    return t[len(ev) + 1:] if t.startswith(ev + "-") else t


def _profit_fields(h: dict, forgone: float, n_markets: int) -> dict:
    """A halt row's quotes, the share of its markets that had any (what the
    forgone reward counts on), blocked $, forgone and net."""
    quotes = h.get("quotes") or []
    share = min(1.0, len({q[0] for q in quotes}) / max(1, n_markets))
    blocked = h.get("blocked", 0.0 if not quotes else None)
    f_eff = forgone * share
    return dict(quotes=quotes, share=share, blocked=blocked,
                blocked_ct=h.get("blocked_ct") or 0.0, forgone_eff=f_eff,
                net=None if blocked is None else -blocked - f_eff)


def build_report(day, status_dir=None, with_profit: bool = True,
                 with_marks: bool = True) -> dict:
    """The day's report. with_marks=False skips the price during each side
    halt (the 7-day line needs only the profit, and the marks sink is the
    biggest read)."""
    start, end = day_window(day)
    rows, seen_file = load_records(start, end, status_dir)
    picks = [r for r in rows if r.get("kind") == "pickoff"]
    sides = [r for r in rows if r.get("kind") == "side_halt"]
    events = [r for r in rows if r.get("kind") == "event_halt"]
    marks = load_marks({r["ticker"] for r in sides} if with_marks else set(),
                       start, end, status_dir)
    ok = None
    if with_profit:
        pickoff_sizes(picks, start, end, status_dir)
        halt_quotes(sides + events, status_dir)
        ok = price_profit(picks, sides + events)
    side_rows = []
    for h in sides:
        prior = [p for p in picks if p["ticker"] == h["ticker"]
                 and p["side"] == h["side"] and p["ts"] <= h["ts"]
                 and h["ts"] - p["ts"] <= float(h.get("window_secs") or 86400)]
        m0 = next((p["mark"] for p in reversed(prior)), None)
        m1 = mark_at(marks, h["ticker"], float(h["until"]))
        move = None
        if m0 is not None and m1 is not None:
            move = (m0 - m1) if h["side"] == "bid" else (m1 - m0)
        halt_s = float(h["until"]) - float(h["ts"])
        forgone = float(h.get("est_per_day") or 0.0) * halt_s / 86400.0
        side_rows.append(dict(
            ts=h["ts"], ticker=h["ticker"], event=h.get("event") or "",
            side=h["side"], picks=[(p["fill_px"], p["mark"]) for p in prior],
            pick_rows=prior, m0=m0, m1=m1, move=move, halt_min=halt_s / 60.0,
            forgone=forgone, **_profit_fields(h, forgone, 1)))
    event_rows = []
    for h in events:
        halt_s = float(h["until"]) - float(h["ts"])
        ev = h["event"]
        picked = []
        for t in h.get("markets_picked") or []:
            ps = [p for p in picks if p["ticker"] == t and p["ts"] <= h["ts"]
                  and h["ts"] - p["ts"] <= float(h.get("window_secs") or 3600)]
            side = ps[-1]["side"] if ps else "?"
            picked.append(f"{_short(t, ev)} ({side})")
        halted = int(h.get("markets_halted") or 0)
        forgone = float(h.get("est_per_day") or 0.0) * halt_s / 86400.0
        event_rows.append(dict(
            ts=h["ts"], event=ev, picked=picked,
            halted=halted, halt_min=halt_s / 60.0,
            est=float(h.get("est_per_day") or 0.0),
            forgone=forgone, **_profit_fields(h, forgone, halted)))
    by_mkt = defaultdict(list)
    for p in picks:
        by_mkt[(p["ticker"], p["side"])].append(p)
    top = sorted(by_mkt.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:15]
    ctx = dict(
        day=day, seen_file=seen_file, n_picks=len(picks),
        n_pick_mkts=len({p["ticker"] for p in picks}),
        side_rows=side_rows, event_rows=event_rows,
        n_side_mkts=len({r["ticker"] for r in side_rows}),
        n_events=len({r["event"] for r in event_rows}),
        forgone=sum(r["forgone"] for r in side_rows + event_rows),
        side_hours=sum(r["halt_min"] for r in side_rows) / 60.0,
        top=[(t, s, ps) for (t, s), ps in top])
    if with_profit:
        ctx["profit"] = profit_totals(side_rows + event_rows, picks, ok)
    return ctx


def profit_totals(halt_rows: list, picks: list, ok: bool) -> dict:
    """The day's PROFIT block (and what toxic_halts_profit.json keeps)."""
    priced = [r for r in halt_rows if r["net"] is not None]
    matched = [p for p in picks if p.get("count") is not None]
    marked = [p for p in matched if p.get("now") is not None]
    ex = [p for p in marked if p.get("exempt")]
    return dict(
        ok=bool(ok), n_halts=len(halt_rows), n_priced=len(priced),
        n_quoted=sum(1 for r in halt_rows if r["quotes"]),
        halts_net=round(sum(r["net"] for r in priced), 4),
        blocked=round(sum(r["blocked"] for r in priced), 4),
        blocked_ct=round(sum(r["blocked_ct"] for r in priced), 2),
        forgone=round(sum(r["forgone_eff"] for r in priced), 4),
        n_picks=len(picks), n_matched=len(matched),
        pick_ct=round(sum(p["count"] for p in matched), 2),
        picks_5m=round(sum(p["usd5"] for p in matched), 4),
        n_marked=len(marked), picks_now=round(sum(p["now"] for p in marked), 4),
        n_exempt=len(ex), exempt_now=round(sum(p["now"] for p in ex), 4),
        marked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))


# ---- the 7-day line -----------------------------------------------------------------

def load_store() -> dict:
    try:
        with open(store_path(), encoding="utf-8") as f:
            return json.load(f).get("days") or {}
    except (OSError, ValueError):
        return {}


def save_store(days: dict) -> None:
    tmp = store_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"days": dict(sorted(days.items()))}, f, indent=1)
    os.replace(tmp, store_path())


def trail_for(day, ctx: dict, save: bool, status_dir=None) -> dict:
    """The TRAIL_DAYS ET days ending `day`, summed: kept days from
    toxic_halts_profit.json, `day` from ctx, a missing day built from the
    sinks (a day with nothing in them adds nothing). `save` (the scheduled
    run) keeps each fully read day."""
    store = load_store()
    days, changed = {}, False
    for k in range(TRAIL_DAYS - 1, -1, -1):
        d = day - timedelta(days=k)
        key = d.isoformat()
        if k == 0:
            s = ctx["profit"]
        elif key in store:
            s = store[key]
        else:
            c = build_report(d, status_dir, with_marks=False)
            if not (c["n_picks"] or c["side_rows"] or c["event_rows"]):
                continue
            s = c["profit"]
        days[key] = s
        if save and s.get("ok") and store.get(key) != s and (k == 0 or key not in store):
            store[key] = s
            changed = True
    if changed:
        try:
            save_store(store)
        except OSError as e:
            log(f"! toxic-halts profit store not written ({e!r})")
    keys = sorted(days)
    tot = {f: sum(float(days[k].get(f) or 0.0) for k in keys)
           for f in ("halts_net", "blocked", "forgone", "picks_now", "n_halts")}
    return dict(n_days=len(keys), first=keys[0] if keys else None,
                last=keys[-1] if keys else None,
                partial=sum(1 for k in keys if not days[k].get("ok")), **tot)


# ---- rendering -----------------------------------------------------------------------

def usd(x) -> str:
    """+$1.25 / -$12.40 / -$1,234; $0.00; n/a."""
    if x is None:
        return "n/a"
    if abs(x) < 0.005:
        return "$0.00"
    a = abs(x)
    return ("-$" if x < 0 else "+$") + (f"{a:,.2f}" if a < 100 else f"{a:,.0f}")


def amt(x) -> str:
    """$ without a sign: $1.25 / $1,234 / $0.00; n/a."""
    return "n/a" if x is None else usd(abs(x)).lstrip("+")


def _blocked_txt(x, n) -> str:
    if x is None:
        return "n/a (no Kalshi read)"
    if not n:
        return "no trade went through their quotes"
    word = "would have lost" if x < 0 else "would have made"
    return f"the fills they blocked {word} {amt(x)} ({n:,.0f} contracts)"


def profit_lines(ctx) -> list:
    """The PROFIT block: the day, then the last 7 days, then how."""
    pr, tr = ctx.get("profit"), ctx.get("trail")
    if not pr:
        return []
    asof = datetime.fromisoformat(pr["marked_at"]).astimezone(imm.ET)
    L = [f"PROFIT (marked to Kalshi's prices at {asof:%a %H:%M} ET; the settlement where "
         f"settled; + = made money)"]
    if not pr["n_halts"]:
        L.append("  Halts: none, so no profit or cost.")
    elif not pr["n_priced"]:
        L.append("  Halts: n/a -- the Kalshi reads failed.")
    elif not pr["n_quoted"]:
        L.append("  Halts: $0.00 -- none had quotes up when it fired or as it ended "
                 "(quiet hours, a position cap), so they changed nothing.")
    else:
        part = (f" ({pr['n_priced']} of {pr['n_halts']} halts priced)"
                if pr["n_priced"] < pr["n_halts"] else "")
        L.append(f"  Halts: net {usd(pr['halts_net'])}{part} -- "
                 f"{_blocked_txt(pr['blocked'], pr['blocked_ct'])}; reward forgone "
                 f"{amt(pr['forgone'])}.")
    # no pick-off total here or in the 7-day line (see the docstring): the
    # most-picked-off table names the markets
    if tr and tr["n_days"]:
        span = (f"{datetime.fromisoformat(tr['first']):%b %d}-"
                f"{datetime.fromisoformat(tr['last']):%b %d}")
        L.append(f"  Last {TRAIL_DAYS} days ({span}, {tr['n_days']} with data): halts net "
                 f"{usd(tr['halts_net'])} over {tr['n_halts']:.0f} halts (blocked fills "
                 f"{usd(tr['blocked'])}, reward forgone {amt(tr['forgone'])})"
                 + (f"; {tr['partial']} day(s) partly read" if tr["partial"] else "") + ".")
    if not pr["ok"]:
        L.append("  ! Some Kalshi reads failed: those markets read n/a.")
    L.append("  How: a halt's net = -(what the fills it blocked would have made) - reward "
             "forgone. Blocked fills = public trades during the halt at or through the "
             "quotes it pulled (or put back as it ended), capped at their size -- the most "
             "it could have saved or cost. Forgone = the bot's estimate, on the halted "
             "markets that had quotes up (none up = quiet hours, a cap: no cost).")
    return L


def subject_for(ctx) -> str:
    d = ctx["day"].strftime("%a %b %d")
    pr = ctx.get("profit")
    if not ctx["side_rows"] and not ctx["event_rows"]:
        return f"IMM toxic halts {d}: none ({ctx['n_picks']} pick-off(s))"
    head = (f"halts {usd(pr['halts_net']) if pr['n_priced'] else 'n/a'} net | "
            if pr else "")
    return (f"IMM toxic halts {d}: {head}{len(ctx['event_rows'])} event, "
            f"{len(ctx['side_rows'])} side ({ctx['n_picks']} pick-offs)")


def _rule_lines() -> list:
    lines = [
        f"Rule: a pick-off = one of our fills with the price "
        f"{imm.TOXIC_PICKOFF_CENTS:g}c+ against it "
        f"{imm.TOXIC_CONFIRM_SECS // 60} min later (pads/takers excluded).",
        f"  Event halt: pick-offs on {imm.TOXIC_EVENT_MARKETS} markets of one "
        f"event within {imm.TOXIC_EVENT_WINDOW_SECS / 60:g} min -> the whole "
        f"event off {imm.TOXIC_EVENT_HALT_SECS / 60:g} min.",
        f"  Side halt: {imm.TOXIC_PICKOFFS} pick-offs on one side of a market "
        f"within {imm.TOXIC_WINDOW_SECS / 3600:g}h -> that side off "
        f"{imm.TOXIC_HALT_SECS / 60:g} min."]
    if imm.TOXIC_EXEMPT_WORDS:
        # Jack 2026-10-09: "turn off toxic halts on MENTION markets"
        lines.append(f"  Exempt: series containing "
                     f"{' / '.join(imm.TOXIC_EXEMPT_WORDS)} -- their pick-offs "
                     f"are listed below but never halt anything.")
    return lines


def _exempt_tag(ps) -> str:
    """' (exempt)' when the pick-offs are on an exempt series."""
    return " (exempt)" if any(p.get("exempt") for p in ps) else ""


def _c(v) -> str:
    """A price in cents, half cents kept: 51 -> '51', 50.5 -> '50.5'."""
    return f"{float(v):.1f}".rstrip("0").rstrip(".")


def _move_txt(r) -> str:
    if r["move"] is None:
        return "n/a"
    if abs(r["move"]) < 0.05:
        return f"{_c(r['m0'])}->{_c(r['m1'])} (flat)"
    word = "kept moving against" if r["move"] > 0 else "moved back"
    return f"{_c(r['m0'])}->{_c(r['m1'])} ({word} {_c(abs(r['move']))}c)"


def _quotes_txt(r, event: bool = False) -> str:
    """Side: '60 @ 54 pulled'; event: '7/9 mkts pulled'; 'none up'."""
    qs = r.get("quotes") or []
    if not qs:
        return "none up"
    hows = sorted({q[4] for q in qs})
    how = hows[0] if len(hows) == 1 else "pulled/put back"
    if event:
        return f"{len({q[0] for q in qs})}/{r['halted']} mkts {how}"
    return ", ".join(f"{q[3]:.0f} @ {_c(q[2])}" for q in qs[:3]) + f" {how}"


def _picks_txt(r) -> str:
    out = []
    for p in r.get("pick_rows") or []:
        s = f"{_c(p['fill_px'])}->{_c(p['mark'])}"
        if p.get("count") is not None:
            s += f" {p['count']:.0f}ct {usd(p['usd5'])}"
        out.append(s)
    return ", ".join(out) or ", ".join(f"{_c(a)}->{_c(b)}" for a, b in r["picks"]) or "n/a"


def _top_usd(ps) -> str:
    """Contracts and $ (5-min check, now) of a market's pick-offs."""
    m = [p for p in ps if p.get("count") is not None]
    if not m:
        return ""
    now = [p["now"] for p in m if p.get("now") is not None]
    return (f"  {sum(p['count'] for p in m):.0f}ct {usd(sum(p['usd5'] for p in m))} at 5 min"
            + (f", {usd(sum(now))} now" if now else ""))


def render_text(ctx) -> str:
    L = [f"IMM toxic-flow halts -- {ctx['day']:%a %Y-%m-%d} (ET day)", ""]
    pl = profit_lines(ctx)
    if pl:
        L += pl + [""]
    L += _rule_lines() + [""]
    if not ctx["seen_file"]:
        L += ["No toxic_halts sink file for this day -- the rule was off, or "
              "the bot wrote nothing (check IMM_TOXIC_HALT / the analytics "
              "sinks).", ""]
    L.append(f"Totals: {ctx['n_picks']} pick-off(s) on {ctx['n_pick_mkts']} "
             f"market(s); {len(ctx['event_rows'])} event halt(s) on "
             f"{ctx['n_events']} event(s); {len(ctx['side_rows'])} side halt(s) "
             f"on {ctx['n_side_mkts']} market(s); est reward forgone "
             f"${ctx['forgone']:.2f}.")
    if not ctx["side_rows"] and not ctx["event_rows"]:
        L += ["", "Nothing was halted."]
    prof = "profit" in ctx
    if ctx["event_rows"]:
        L += ["", "EVENT HALTS (whole event, both sides)"]
        if prof:
            L.append(f"  {'ET':5s}  {'event':34s} {'mkts':>4s} {'quotes':18s} "
                     f"{'blocked':>8s} {'forgone':>8s} {'net':>8s}  picked off")
        else:
            L.append(f"  {'ET':5s}  {'event':34s} {'mkts':>4s} {'forgone':>8s}  picked off")
        for r in ctx["event_rows"][:MAX_ROWS]:
            if prof:
                L.append(f"  {_et(r['ts']):5s}  {r['event'][:34]:34s} {r['halted']:4d} "
                         f"{_quotes_txt(r, True)[:18]:18s} {usd(r['blocked']):>8s} "
                         f"{'$%.2f' % r['forgone_eff']:>8s} {usd(r['net']):>8s}  "
                         f"{', '.join(r['picked'])}")
            else:
                L.append(f"  {_et(r['ts']):5s}  {r['event'][:34]:34s} {r['halted']:4d} "
                         f"{'$%.2f' % r['forgone']:>8s}  {', '.join(r['picked'])}")
    if ctx["side_rows"]:
        L += ["", "SIDE HALTS"]
        if prof:
            L.append(f"  {'ET':5s}  {'market':40s} {'side':4s} {'quote':18s} "
                     f"{'blocked':>8s} {'forgone':>8s} {'net':>8s}  "
                     f"pick-offs (fill->mark, contracts, $ at 5 min)  price during halt")
        else:
            L.append(f"  {'ET':5s}  {'market':40s} {'side':4s} {'forgone':>8s}  "
                     f"pick-offs (fill->mark)  price during halt")
        for r in ctx["side_rows"][:MAX_ROWS]:
            if prof:
                L.append(f"  {_et(r['ts']):5s}  {r['ticker'][:40]:40s} "
                         f"{r['side'].upper():4s} {_quotes_txt(r)[:18]:18s} "
                         f"{usd(r['blocked']):>8s} {'$%.2f' % r['forgone_eff']:>8s} "
                         f"{usd(r['net']):>8s}  {_picks_txt(r)}  {_move_txt(r)}")
            else:
                pk = ", ".join(f"{_c(a)}->{_c(b)}" for a, b in r["picks"]) or "n/a"
                L.append(f"  {_et(r['ts']):5s}  {r['ticker'][:40]:40s} "
                         f"{r['side'].upper():4s} {'$%.2f' % r['forgone']:>8s}  "
                         f"{pk}  {_move_txt(r)}")
    if prof and (ctx["event_rows"] or ctx["side_rows"]):
        L.append("  (blocked = what the fills a halt blocked would have made, + or -; "
                 "net = -blocked - forgone, + = the halt paid)")
    if ctx["top"]:
        L += ["", "MOST PICKED-OFF (including ones that never halted)"]
        for t, s, ps in ctx["top"]:
            worst = max(ps, key=lambda p: abs(p["mark"] - p["fill_px"]))
            L.append(f"  {len(ps):3d}x  {t[:44]:44s} {s.upper():4s} worst "
                     f"{_c(worst['fill_px'])}->{_c(worst['mark'])}{_exempt_tag(ps)}"
                     f"{_top_usd(ps)}")
    L += ["", "Knobs: IMM_TOXIC_HALT=0 kills both rules; IMM_TOXIC_EVENT_MARKETS=0 "
          "the event rule alone; IMM_TOXIC_PICKOFF_CENTS / _PICKOFFS / "
          "_HALT_MIN / _EVENT_WINDOW_MIN / _EVENT_HALT_MIN tune them; "
          "IMM_TOXIC_EXEMPT lists the exempt series words."]
    return "\n".join(L)


def render_html(ctx) -> str:
    def table(head, rows, left=None):
        h = "".join(f'<th style="{TDL}background:#f3f3f3">{_esc(x)}</th>' for x in head)
        body = "".join("<tr>" + "".join(
            f'<td style="{TDL if i in (left or (1, len(r) - 1)) else TD}">{_esc(str(c))}</td>'
            for i, c in enumerate(r)) + "</tr>" for r in rows)
        return f'<table style="border-collapse:collapse;font:13px sans-serif">' \
               f'<tr>{h}</tr>{body}</table>'
    parts = [f"<h3 style='font:bold 15px sans-serif'>IMM toxic-flow halts -- "
             f"{ctx['day']:%a %Y-%m-%d} (ET)</h3>"]
    pl = profit_lines(ctx)
    if pl:
        parts.append("<p style='font:13px sans-serif'><b>" + _esc(pl[0]) + "</b><br>"
                     + "<br>".join(_esc(x.strip()) for x in pl[1:]) + "</p>")
    parts.append("<p style='font:13px sans-serif'>" + "<br>".join(
        _esc(x) for x in _rule_lines()) + "</p>")
    if not ctx["seen_file"]:
        parts.append("<p style='font:13px sans-serif;color:#a00'>No toxic_halts "
                     "sink file for this day -- the rule was off, or nothing was "
                     "written.</p>")
    parts.append(
        f"<p style='font:13px sans-serif'><b>{ctx['n_picks']}</b> pick-off(s) on "
        f"{ctx['n_pick_mkts']} market(s); <b>{len(ctx['event_rows'])}</b> event "
        f"halt(s); <b>{len(ctx['side_rows'])}</b> side halt(s); est reward "
        f"forgone <b>${ctx['forgone']:.2f}</b>.</p>")
    prof = "profit" in ctx
    if ctx["event_rows"]:
        parts.append("<h4 style='font:bold 13px sans-serif'>Event halts (whole "
                     "event, both sides)</h4>")
        if prof:
            parts.append(table(["ET", "event", "markets", "quotes", "blocked", "forgone",
                                "net", "picked off"], [
                (_et(r["ts"]), r["event"], r["halted"], _quotes_txt(r, True),
                 usd(r["blocked"]), f"${r['forgone_eff']:.2f}", usd(r["net"]),
                 ", ".join(r["picked"])) for r in ctx["event_rows"][:MAX_ROWS]],
                left=(1, 3, 7)))
        else:
            parts.append(table(["ET", "event", "markets", "forgone", "picked off"], [
                (_et(r["ts"]), r["event"], r["halted"], f"${r['forgone']:.2f}",
                 ", ".join(r["picked"])) for r in ctx["event_rows"][:MAX_ROWS]]))
    if ctx["side_rows"]:
        parts.append("<h4 style='font:bold 13px sans-serif'>Side halts</h4>")
        if prof:
            parts.append(table(["ET", "market", "side", "quote", "blocked", "forgone", "net",
                                "pick-offs (fill->mark, contracts, $ at 5 min)",
                                "price during halt"], [
                (_et(r["ts"]), r["ticker"], r["side"].upper(), _quotes_txt(r),
                 usd(r["blocked"]), f"${r['forgone_eff']:.2f}", usd(r["net"]),
                 _picks_txt(r), _move_txt(r)) for r in ctx["side_rows"][:MAX_ROWS]],
                left=(1, 3, 7, 8)))
        else:
            parts.append(table(["ET", "market", "side", "forgone", "pick-offs (fill->mark)",
                                "price during halt"], [
                (_et(r["ts"]), r["ticker"], r["side"].upper(), f"${r['forgone']:.2f}",
                 ", ".join(f"{_c(a)}->{_c(b)}" for a, b in r["picks"]) or "n/a",
                 _move_txt(r)) for r in ctx["side_rows"][:MAX_ROWS]]))
    if prof and (ctx["event_rows"] or ctx["side_rows"]):
        parts.append("<p style='font:12px sans-serif;color:#555'>blocked = what the fills a "
                     "halt blocked would have made (+) or lost (-); net = -blocked - "
                     "forgone, + = the halt paid.</p>")
    if not ctx["side_rows"] and not ctx["event_rows"]:
        parts.append("<p style='font:13px sans-serif'>Nothing was halted.</p>")
    if ctx["top"]:
        parts.append("<h4 style='font:bold 13px sans-serif'>Most picked-off "
                     "(including ones that never halted)</h4>")
        rows = []
        for t, s, ps in ctx["top"]:
            worst = max(ps, key=lambda p: abs(p["mark"] - p["fill_px"]))
            rows.append((len(ps), t + _exempt_tag(ps), s.upper(),
                         f"{_c(worst['fill_px'])}->{_c(worst['mark'])}",
                         _top_usd(ps).strip()))
        parts.append(table(["count", "market", "side", "worst", "contracts, $"], rows))
    return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true",
                    help="send now regardless of the sent-marker; no marker written")
    ap.add_argument("--dry", action="store_true",
                    help="build and print only; no email, no marker")
    ap.add_argument("--day", help="ET day to report (YYYY-MM-DD); default yesterday")
    args = ap.parse_args(argv)

    now_utc = datetime.now(timezone.utc)
    today_et = now_utc.astimezone(imm.ET).date()
    day = (datetime.strptime(args.day, "%Y-%m-%d").date() if args.day
           else today_et - timedelta(days=1))
    marker = os.path.join(imm.STATUS_DIR, f"imm_toxic_halts_sent_{today_et}.marker")
    scheduled = not (args.test or args.dry or args.day)
    if scheduled and os.path.exists(marker):
        log(f"toxic-halts email already sent for {today_et}; exiting")
        return 0
    low_priority()
    ctx = build_report(day)
    ctx["trail"] = trail_for(day, ctx, save=scheduled)
    text, html, subject = render_text(ctx), render_html(ctx), subject_for(ctx)
    log("toxic-halts body:\n" + text)
    if args.dry:
        return 0
    attempts = 1 if args.test else 8
    alerter = imm.Alerter("IMM-TOXIC", live=True)
    if not alerter.enabled:
        log("cannot send toxic-halts email: alert credentials not configured")
        return 1
    ok = False
    for attempt in range(1, attempts + 1):
        ok = alerter.send_message(text, subject=subject, html=html)
        if ok:
            break
        log(f"toxic-halts send attempt {attempt}/{attempts} failed; retry 5min")
        if attempt < attempts:
            time.sleep(300)
    log(f"toxic-halts send: {'ok' if ok else 'FAILED'}")
    if not ok:
        return 1
    if not (args.test or args.day):
        try:
            with open(marker, "w") as f:
                f.write(now_utc.isoformat())
        except OSError as e:
            log(f"! sent-marker not written ({e!r}); a re-run today would send again")
        cutoff = today_et - timedelta(days=7)
        for old in glob.glob(os.path.join(imm.STATUS_DIR,
                                          "imm_toxic_halts_sent_*.marker")):
            name = os.path.basename(old)[len("imm_toxic_halts_sent_"):-len(".marker")]
            try:
                if datetime.strptime(name, "%Y-%m-%d").date() < cutoff:
                    os.remove(old)
            except (ValueError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
