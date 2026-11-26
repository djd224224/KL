#!/usr/bin/env python3
"""The MLB next team nights pilot's morning email (Jack 2026-11-26: "give
email daily report", the day the pilot went live: d032033, KXMLBNEXTTEAM
quoted 00:00-07:00 ET only, x3, to the programs' 12/03 05:00Z end).

Task Scheduler job "KL imm mlb-nights" runs it at 07:15 ET, after each
night's window closes (run_mlb_nights_report.ps1, hidden). Every number comes
from the bot's own sinks in STATUS_DIR, read-only, plus one batch of public
market reads for the current prices; nothing here imports incentive_mm.
  - REWARDS, MODELLED: the cycle log's est_frac x pool_per_day over the gap
    since the previous cycle, capped at 900 s -- the reconciler's own rule
    (imm_reward_recon._scan_cycle_log). A market writes a cycle row only while
    it is quoted, so the accrual is the window's alone.
  - TRADING, MEASURED: every pilot fill (fills_*.jsonl) marked to Kalshi's
    last trade now (the dashboard's valuation since 2026-11-10), or to 100/0
    once the market settles -- mark-to-market including unsettled positions,
    before fees.
  - STOPS: the guard sink's entries (fill pauses, halts), sweep-breaker trips
    (ws_sweep_*.jsonl), the family's loss-halt day (imm_state.json) and the
    stop file.
The "night" of day D is D 00:00-07:00 ET; "since start" is from the first
night (11/27 00:00 ET) to the run. A sent-marker per day keeps it to one
email; --force resends, --dry builds and prints without sending, --test
prefixes the subject. Exit 0 sent / dry / already sent, 1 send failed.

  python send_mlb_nights_report.py --dry
  python send_mlb_nights_report.py --day 2026-11-27 --test
"""
from __future__ import annotations

import argparse
import csv
import html as _html
import json
import os
import smtplib
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_logfiles  # noqa: E402

ET = ZoneInfo("America/New_York")
STATUS_DIR = os.environ.get("IMM_STATUS_DIR", r"C:\Users\jackd\Documents\KL\run-logs\incentive-mm")
OUT_DIR = os.path.join(STATUS_DIR, "mlb_nights")
SERIES = "KXMLBNEXTTEAM"
LABEL = "MLB next team"
FIRST_NIGHT = date(2026, 11, 27)
PILOT_END = datetime(2026, 12, 3, 5, 0, tzinfo=timezone.utc)
WINDOW_ET = ("00:00", "07:00")
STOP_FILE = "astest_mlb_stop.txt"
HALT_KEY = "ASTEST:MLB"
# the bot's own floor projection at the go-live (47 markets, 14:58Z 11/26):
# shown as the reference a night is read against, MODELLED like the rewards
MODEL_PER_NIGHT = 20.77
MAX_DT = 900.0
MOVE_CENTS = 15.0
SEND_TRIES, SEND_WAIT = 3, 20

INK, MUTED, LINE, GOOD, BAD = "#1f2328", "#59636e", "#d1d9e0", "#1a7f37", "#cf222e"
S_TD = f"padding:5px 8px;border-top:1px solid {LINE};font-size:13px;vertical-align:top"
S_TH = f"padding:5px 8px;border-bottom:2px solid {LINE};font-size:12px;color:{MUTED};font-weight:600;text-align:left"


def setting(name: str) -> str:
    v = os.environ.get(name, "")
    if v:
        return v
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, name)[0] or "")
    except Exception:
        return ""


def et_instant(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=ET).astimezone(timezone.utc)


def night_bounds(day: date, window: Tuple[str, str] = WINDOW_ET) -> Tuple[datetime, datetime]:
    """[start, end) UTC of day D's quote window ("24:00" = the next midnight)."""
    start = et_instant(day, window[0])
    end = (et_instant(day + timedelta(days=1), "00:00") if window[1] == "24:00"
           else et_instant(day, window[1]))
    return start, end


def utc_dates(start: datetime, end: datetime) -> List[date]:
    out, d = [], start.date()
    while d <= (end - timedelta(microseconds=1)).date():
        out.append(d)
        d += timedelta(days=1)
    return out


def _day_path(prefix: str, d: date, ext: str) -> Optional[str]:
    p = imm_logfiles.resolve(os.path.join(STATUS_DIR, f"{prefix}_{d.isoformat()}.{ext}"))
    return p if p and os.path.exists(p) else None


def _ts(s) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def series_of(t: str) -> str:
    return t.split("-")[0]


def event_of(t: str) -> str:
    parts = t.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else t


def short(t: str) -> str:
    """'KXMLBNEXTTEAM-26GSPRINGER4-TOR' -> 'GSPRINGER to TOR'."""
    parts = t.split("-")
    if len(parts) < 3:
        return t
    who = parts[1][2:].rstrip("0123456789") or parts[1]
    return f"{who} to {parts[2]}"


# the guard sink's names in plain words (the rest show as they are)
GUARD_WORDS = {
    "astest_fill_pause": "fill pause (event aside 15 min)",
    "twk_halt": "loss halt ($100 budget)",
    "sweep_trip": "sweep breaker (event pulled)",
    "move_breaker": "mid-move breaker",
    "fill_burst": "fill burst",
    "manual_yield": "your hand trades (bot yields)",
}


# ---- the sinks ------------------------------------------------------------

def load_fills(series: str, start: datetime, end: datetime) -> List[dict]:
    """Our fills on `series` stamped in [start, end), one per fill_id."""
    seen, out = set(), []
    for d in utc_dates(start, end):
        p = _day_path("fills", d, "jsonl")
        if not p:
            continue
        with imm_logfiles.open_text(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if series not in line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                ts = _ts(r.get("ts"))
                if r.get("series") != series or ts is None or not (start.timestamp() <= ts < end.timestamp()):
                    continue
                fid = r.get("fill_id") or f"{r.get('order_id')}:{ts}:{r.get('count')}"
                if fid in seen:
                    continue
                seen.add(fid)
                out.append(r)
    out.sort(key=lambda r: _ts(r.get("ts")) or 0)
    return out


def fill_sign(r: dict) -> float:
    """+1 when the fill made us longer YES, -1 shorter."""
    obs = r.get("our_book_side")
    if obs in ("bid", "ask"):
        return 1.0 if obs == "bid" else -1.0
    return 1.0 if (r.get("side") == "yes") == (r.get("action") == "buy") else -1.0


def _scan_day(path: str, series: str) -> Dict[str, Dict[str, list]]:
    """{ticker: {utc_hour_index: [est, cycles, first_mid, last_mid, max_bid_ct,
    max_ask_ct]}} for one cycle-log day file: the reconciler's accrual (each
    cycle's est_frac x pool_per_day over the gap since the previous DISTINCT
    timestamp, capped at MAX_DT; the day's first cycle gets no gap, its
    cross-file seam), bucketed by the hour of the cycle it lands on, the
    series' rows only. A day file is ~600 MB: every line's timestamp is read
    off its first 20 characters and only the series' lines are parsed."""
    out: Dict[str, Dict[str, list]] = {}
    tag = "," + series + "-"
    ts_cache: Dict[str, float] = {}
    prev_ts = cur_ts = None
    batch: List[list] = []

    def flush(ts, rows, dt):
        hour = str(int(ts // 3600))
        for row in rows:
            try:
                frac, pool = float(row[7]), float(row[11])
                bid, ask = float(row[2] or 0), float(row[3] or 0)
                bct = float(row[13] or 0) if len(row) > 14 else 0.0
                act = float(row[14] or 0) if len(row) > 14 else 0.0
            except (ValueError, IndexError):
                continue
            h = out.setdefault(row[1], {}).setdefault(hour, [0.0, 0, None, None, 0.0, 0.0])
            h[0] += frac * pool * dt / 86400.0
            h[1] += 1
            if 0 < bid < ask:
                mid = (bid + ask) / 2.0
                if h[2] is None:
                    h[2] = mid
                h[3] = mid
            h[4] = max(h[4], bct)
            h[5] = max(h[5], act)

    with imm_logfiles.open_text(path, newline="", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            s = line[:20]
            ts = ts_cache.get(s)
            if ts is None:
                try:
                    ts = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc).timestamp()
                except ValueError:
                    continue                       # the header, a torn line
                ts_cache[s] = ts
            if cur_ts is None:
                cur_ts = ts
            elif ts != cur_ts:
                if prev_ts and batch:
                    dt = min(cur_ts - prev_ts, MAX_DT)
                    if dt > 0:
                        flush(cur_ts, batch, dt)
                prev_ts, cur_ts, batch = cur_ts, ts, []
            if tag in line[:80]:
                row = next(csv.reader([line]))
                if len(row) >= 13 and row[1].startswith(series + "-"):
                    batch.append(row)
        if batch and prev_ts:
            dt = min(cur_ts - prev_ts, MAX_DT)
            if dt > 0:
                flush(cur_ts, batch, dt)
    return out


def _day_hours(d: date, series: str, today: date) -> Dict[str, Dict[str, list]]:
    """The day's hourly buckets, cached in OUT_DIR for days before `today`
    (a finished day file no longer changes; keyed by its signature)."""
    p = _day_path("cycle_log", d, "csv")
    if not p:
        return {}
    cache_path = os.path.join(OUT_DIR, f"cycle_cache_{series}.json")
    cache: dict = {}
    if d < today:
        try:
            with open(cache_path, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (OSError, ValueError):
            cache = {}
        hit = cache.get(d.isoformat())
        if hit and hit.get("sig") == imm_logfiles.sig(p):
            return hit["hours"]
    hours = _scan_day(p, series)
    if d < today:
        cache[d.isoformat()] = {"sig": imm_logfiles.sig(p), "hours": hours}
        try:
            os.makedirs(OUT_DIR, exist_ok=True)
            with open(cache_path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump(cache, fh)
            os.replace(cache_path + ".tmp", cache_path)
        except OSError as e:
            print(f"cycle cache write failed: {type(e).__name__}")
    return hours


def scan_cycles(series: str, start: datetime, end: datetime,
                today: Optional[date] = None) -> Dict[str, dict]:
    """{ticker: {est, cycles, first_mid, last_mid, max_bid_ct, max_ask_ct}}
    over the cycles whose hour starts in [start, end) (the windows run on
    whole hours)."""
    today = today or datetime.now(timezone.utc).date()
    lo, hi = start.timestamp(), end.timestamp()
    out: Dict[str, dict] = {}
    for d in utc_dates(start, end):
        for t, hours in _day_hours(d, series, today).items():
            for hk in sorted(hours, key=int):
                h0 = int(hk) * 3600
                if not (lo <= h0 < hi):
                    continue
                v = hours[hk]
                m = out.setdefault(t, {"est": 0.0, "cycles": 0, "first_mid": None,
                                       "last_mid": None, "max_bid_ct": 0.0, "max_ask_ct": 0.0})
                m["est"] += v[0]
                m["cycles"] += v[1]
                if v[2] is not None and m["first_mid"] is None:
                    m["first_mid"] = v[2]
                if v[3] is not None:
                    m["last_mid"] = v[3]
                m["max_bid_ct"] = max(m["max_bid_ct"], v[4])
                m["max_ask_ct"] = max(m["max_ask_ct"], v[5])
    return out


def load_guards(series: str, start: datetime, end: datetime) -> Dict[str, dict]:
    """{guard: {"enters": n, "markets": set, "events": set}} from the guard
    sink's `enter` rows, plus "sweep_trip" from ws_sweep."""
    out: Dict[str, dict] = defaultdict(lambda: {"enters": 0, "markets": set(), "events": set()})
    lo, hi = start.timestamp(), end.timestamp()
    for d in utc_dates(start, end):
        for prefix, kind in (("guard_skips", "guard"), ("ws_sweep", "sweep")):
            p = _day_path(prefix, d, "jsonl")
            if not p:
                continue
            with imm_logfiles.open_text(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if series not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    ts = _ts(r.get("ts"))
                    if ts is None or not (lo <= ts < hi):
                        continue
                    t = str(r.get("ticker") or r.get("event") or "")
                    if series_of(t) != series:
                        continue
                    if kind == "guard":
                        if r.get("kind") != "enter":
                            continue
                        g = str(r.get("guard") or "?")
                    else:
                        g = "sweep_trip"
                    o = out[g]
                    o["enters"] += 1
                    o["markets"].add(t)
                    o["events"].add(event_of(t) if t.count("-") >= 2 else t)
    return dict(out)


def halt_day() -> Optional[str]:
    try:
        with open(os.path.join(STATUS_DIR, "imm_state.json"), encoding="utf-8") as fh:
            st = json.load(fh)
        return (st.get("twk_halt_day") or {}).get(HALT_KEY)
    except (OSError, ValueError, AttributeError):
        return None


def fetch_marks(tickers: List[str]) -> Dict[str, dict]:
    """{ticker: {bid, ask, last, status, result}} in cents, from Kalshi's
    public market reads (kalshi_reads: signed first, paced); {} on failure."""
    out: Dict[str, dict] = {}
    if not tickers:
        return out
    try:
        from kalshi_reads import kalshi_get
    except Exception as e:  # noqa: BLE001
        print(f"marks: kalshi_reads unavailable ({type(e).__name__})")
        return out
    c = lambda v: float(v) * 100.0 if v not in (None, "") else None
    for i in range(0, len(tickers), 20):
        try:
            js = kalshi_get("/markets", {"tickers": ",".join(tickers[i:i + 20]), "limit": 100})
        except Exception as e:  # noqa: BLE001
            print(f"marks: read failed ({type(e).__name__}: {str(e)[:80]})")
            continue
        for m in js.get("markets") or []:
            out[m["ticker"]] = {"bid": c(m.get("yes_bid_dollars")), "ask": c(m.get("yes_ask_dollars")),
                                "last": c(m.get("last_price_dollars")), "status": m.get("status"),
                                "result": m.get("result") or ""}
    return out


def mark_of(t: str, marks: Dict[str, dict], fallback: Optional[float]) -> Tuple[Optional[float], str]:
    """(cents, basis): the settlement once there is a result, else Kalshi's
    last trade, else the mid, else `fallback` (the last cycle mid or the
    fill's own price)."""
    m = marks.get(t) or {}
    if m.get("result") in ("yes", "no"):
        return (100.0 if m["result"] == "yes" else 0.0), "settled"
    if m.get("last") is not None and m["last"] > 0:
        return m["last"], "last"
    if m.get("bid") is not None and m.get("ask") and m["ask"] > (m["bid"] or 0):
        return (m["bid"] + m["ask"]) / 2.0, "mid"
    return fallback, "stale"


# ---- the numbers ------------------------------------------------------------

def summarize(fills: List[dict], cyc: Dict[str, dict], marks: Dict[str, dict]) -> dict:
    """Rewards (modelled), trading P&L at the marks (measured), positions."""
    rew = sum(v["est"] for v in cyc.values())
    quoted = {t for t, v in cyc.items() if v["cycles"] > 0}
    pnl, rows, pos = 0.0, [], defaultdict(lambda: [0.0, 0.0])   # net, cost
    for r in fills:
        t = r.get("ticker") or ""
        n = float(r.get("count") or 0)
        px = float(r.get("yes_price_cents") or 0)
        s = fill_sign(r)
        fb = (cyc.get(t) or {}).get("last_mid")
        mk, basis = mark_of(t, marks, fb if fb is not None else px)
        p = s * ((mk if mk is not None else px) - px) * n / 100.0
        pnl += p
        pos[t][0] += s * n
        pos[t][1] += s * n * px
        rows.append({"ts": _ts(r.get("ts")), "ticker": t, "side": "bought" if s > 0 else "sold",
                     "px": px, "n": n, "mark": mk, "basis": basis, "pnl": p,
                     "taker": bool(r.get("is_taker"))})
    positions = []
    for t, (net, cost) in pos.items():
        if abs(net) < 1e-9:
            continue
        mk, basis = mark_of(t, marks, (cyc.get(t) or {}).get("last_mid"))
        avg = cost / net
        positions.append({"ticker": t, "net": net, "avg": avg, "mark": mk, "basis": basis,
                          "mtm": net * ((mk if mk is not None else avg) - avg) / 100.0})
    positions.sort(key=lambda x: x["mtm"])
    return {"rew": rew, "pnl": pnl, "net": rew + pnl, "fills": rows, "positions": positions,
            "quoted": quoted, "events": {event_of(t) for t in quoted},
            "bought": sum(x["n"] for x in rows if x["side"] == "bought"),
            "sold": sum(x["n"] for x in rows if x["side"] == "sold")}


def moves(cyc: Dict[str, dict], marks: Dict[str, dict]) -> List[dict]:
    """Quoted markets whose price moved MOVE_CENTS or more from their first
    quoted cycle to now, or that settled: where news (a signing) landed."""
    out = []
    for t, v in cyc.items():
        if v["first_mid"] is None:
            continue
        mk, basis = mark_of(t, marks, v["last_mid"])
        if mk is None:
            continue
        if abs(mk - v["first_mid"]) >= MOVE_CENTS or basis == "settled":
            out.append({"ticker": t, "from": v["first_mid"], "to": mk, "basis": basis})
    out.sort(key=lambda x: -abs(x["to"] - x["from"]))
    return out


# ---- the email --------------------------------------------------------------

def usd(x: float, signed: bool = True) -> str:
    return (f"{'+' if x >= 0 else '-'}${abs(x):,.2f}" if signed else f"${x:,.2f}")


def _tbl(head: List[str], rows: List[List[str]]) -> str:
    th = "".join(f'<th style="{S_TH}">{_html.escape(h)}</th>' for h in head)
    tr = "".join("<tr>" + "".join(f'<td style="{S_TD}">{c}</td>' for c in r) + "</tr>" for r in rows)
    return f'<table style="border-collapse:collapse;margin:6px 0 14px 0">{"<tr>" + th + "</tr>"}{tr}</table>'


def _col(x: float) -> str:
    return f'<span style="color:{GOOD if x >= 0 else BAD}">{usd(x)}</span>'


def build(day: date, now: Optional[datetime] = None, marks: Optional[Dict[str, dict]] = None,
          series: str = SERIES, window: Tuple[str, str] = WINDOW_ET) -> Tuple[str, str, str, dict]:
    """(subject, html, text, ctx) for the night of `day`."""
    now = now or datetime.now(timezone.utc)
    n_start, n_end = night_bounds(day, window)
    s_start = night_bounds(FIRST_NIGHT, window)[0]
    s_end = min(now, PILOT_END + timedelta(days=1))
    nf = load_fills(series, n_start, n_end)
    sf = load_fills(series, s_start, s_end)
    ncyc = scan_cycles(series, n_start, n_end)
    scyc = scan_cycles(series, s_start, s_end)
    tickers = sorted({r.get("ticker") for r in sf} | set(scyc))
    marks = fetch_marks(tickers) if marks is None else marks
    night = summarize(nf, ncyc, marks)
    since = summarize(sf, scyc, marks)
    mv = moves(scyc, marks)
    guards = load_guards(series, n_start, n_end)
    nights_done = max(0, (min(day, (PILOT_END - timedelta(minutes=1)).astimezone(ET).date())
                          - FIRST_NIGHT).days + 1)
    nights_left = max(0, ((PILOT_END - timedelta(minutes=1)).astimezone(ET).date() - day).days)
    stopped = os.path.exists(os.path.join(STATUS_DIR, STOP_FILE))
    hd = halt_day()
    status = ("STOPPED (stop file)" if stopped else
              "ENDED" if now >= PILOT_END else
              f"HALTED {hd} (loss budget)" if hd and hd >= day.isoformat() else "ON")
    # per-market accrual since start against the $1 payout floor (one period)
    over = sum(1 for v in scyc.values() if v["est"] >= 1.0)
    on_pace = sum(1 for v in scyc.values()
                  if v["est"] < 1.0 and nights_done and v["est"] / nights_done * (nights_done + nights_left) >= 1.0)
    ctx = {"day": day.isoformat(), "night": night, "since": since, "moves": mv, "guards": guards,
           "status": status, "nights_done": nights_done, "nights_left": nights_left,
           "over_floor": over, "on_pace": on_pace, "markets_since": len(scyc)}
    wd = f"{day:%a %b} {day.day}"
    subject = (f"MLB nights pilot, {wd}: {usd(night['net'])} net "
               f"(rewards {usd(night['rew'], False)} modelled, trading {usd(night['pnl'])}), "
               f"{len(night['fills'])} fill{'s' if len(night['fills']) != 1 else ''}; "
               f"since start {usd(since['net'])}")

    def block(lbl, s):
        return [lbl, f"{len(s['quoted'])} / {len(s['events'])}", f"{len(s['fills'])}",
                f"{s['bought']:,.0f} / {s['sold']:,.0f}", usd(s["rew"], False), _col(s["pnl"]), _col(s["net"])]

    h = [f'<div style="font-family:Segoe UI,Arial,sans-serif;color:{INK};max-width:760px">',
         f'<h2 style="margin:0 0 4px 0;font-size:18px">{LABEL} nights pilot: night of {wd} '
         f'({window[0]}-{window[1]} ET)</h2>',
         f'<p style="margin:0 0 10px 0;color:{MUTED};font-size:13px">{series} x3, quoted '
         f'{window[0]}-{window[1]} ET only, to {PILOT_END:%b %d %H:%M}Z. Status: <b>{status}</b>. '
         f'Night {nights_done} of {nights_done + nights_left}; {nights_left} left.</p>',
         _tbl(["", "markets / events quoted", "fills", "bought / sold ct", "rewards (modelled)",
               "trading (at last trade)", "net"],
              [block("This night", night), block("Since start", since)]),
         f'<p style="margin:0 0 10px 0;font-size:13px">The bot projected about '
         f'${MODEL_PER_NIGHT:.2f} a night of rewards when it went live (modelled). Payout floor: '
         f'{over} market{"s" if over != 1 else ""} past $1 so far, {on_pace} more on pace '
         f'(of {len(scyc)} quoted).</p>']
    if night["fills"]:
        h.append('<h3 style="margin:8px 0 2px 0;font-size:15px">Fills this night</h3>')
        h.append(_tbl(["ET", "market", "", "price", "contracts", "now", "P&L now"],
                      [[datetime.fromtimestamp(f["ts"], ET).strftime("%H:%M"), _html.escape(short(f["ticker"])),
                        f["side"] + (" (taker)" if f["taker"] else ""), f"{f['px']:.0f}c", f"{f['n']:,.0f}",
                        (f"{f['mark']:.0f}c" if f["mark"] is not None else "-") + ("" if f["basis"] == "last" else f" ({f['basis']})"),
                        _col(f["pnl"])] for f in night["fills"]]))
    else:
        h.append(f'<p style="font-size:13px">No fills this night.</p>')
    if since["positions"]:
        h.append('<h3 style="margin:8px 0 2px 0;font-size:15px">Open positions</h3>')
        h.append(_tbl(["market", "net YES", "avg", "now", "MTM"],
                      [[_html.escape(short(p["ticker"])), f"{p['net']:+,.0f}", f"{p['avg']:.1f}c",
                        (f"{p['mark']:.0f}c" if p["mark"] is not None else "-"), _col(p["mtm"])]
                       for p in since["positions"]]))
    if mv:
        h.append(f'<h3 style="margin:8px 0 2px 0;font-size:15px">Price moves of {MOVE_CENTS:.0f}c+ since quoting began (news)</h3>')
        h.append(_tbl(["market", "first quoted mid", "now"],
                      [[_html.escape(short(m["ticker"])), f"{m['from']:.1f}c",
                        f"{m['to']:.0f}c" + (" (settled)" if m["basis"] == "settled" else "")] for m in mv[:15]]))
    g_rows = [[_html.escape(GUARD_WORDS.get(g, g)), f"{v['enters']}", f"{len(v['events'])}"]
              for g, v in sorted(guards.items()) if g != "astest_window"]
    h.append('<h3 style="margin:8px 0 2px 0;font-size:15px">Stops this night</h3>')
    h.append(_tbl(["guard", "entries", "events"], g_rows) if g_rows else
             '<p style="font-size:13px">None fired (no fill pause, halt or sweep trip).</p>')
    h.append(f'<p style="margin:12px 0 0 0;color:{MUTED};font-size:12px">Rewards are the bot\'s '
             f'model (cycle log est_frac x pool, capped 900 s per gap), not Kalshi\'s credit. Trading '
             f'is every pilot fill marked to Kalshi\'s last trade (100/0 once settled), before fees. '
             f'Stop the pilot: create run-logs\\incentive-mm\\{STOP_FILE} (positions ride).</p></div>')
    page = "\n".join(h)
    text = (f"{subject}\nStatus {status}; night {nights_done}, {nights_left} left.\n"
            f"This night: {len(night['quoted'])} markets, {len(night['fills'])} fills, rewards "
            f"{usd(night['rew'], False)} (modelled), trading {usd(night['pnl'])}, net {usd(night['net'])}.\n"
            f"Since start: rewards {usd(since['rew'], False)}, trading {usd(since['pnl'])}, net {usd(since['net'])}.\n")
    return subject, page, text, ctx


def send(subject: str, page: str, text: str) -> bool:
    frm, pw = setting("ALERT_EMAIL_FROM"), setting("ALERT_EMAIL_PASSWORD")
    to = setting("IMM_REVIEW_TO") or setting("ALERT_EMAIL_TO") or frm
    if not frm or not pw:
        print("send: ALERT_EMAIL_FROM / ALERT_EMAIL_PASSWORD not configured")
        return False
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(page, "html", "utf-8"))
    msg["Subject"], msg["From"], msg["To"] = subject, frm, to
    rcpt = [r.strip() for r in to.split(",") if r.strip()]
    for i in range(SEND_TRIES):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(frm, pw)
                s.sendmail(frm, rcpt, msg.as_string())
            print(f"send: ok ({len(rcpt)} recipient(s))")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"send: try {i + 1} failed: {type(e).__name__}")
            if i + 1 < SEND_TRIES:
                time.sleep(SEND_WAIT)
    print("send: FAILED")
    return False


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Email the MLB nights pilot's morning report.")
    ap.add_argument("--day", help="the night's ET date (default: today in ET)")
    ap.add_argument("--dry", action="store_true", help="build and print, no email")
    ap.add_argument("--test", action="store_true", help="send now with [TEST] in the subject")
    ap.add_argument("--force", action="store_true", help="send even if already sent")
    ap.add_argument("--series", default=SERIES, help="(checks) another series' sinks")
    ap.add_argument("--window", default="-".join(WINDOW_ET), help="(checks) HH:MM-HH:MM ET")
    a = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    day = date.fromisoformat(a.day) if a.day else now.astimezone(ET).date()
    if not a.day and (day < FIRST_NIGHT or now > PILOT_END + timedelta(days=1, hours=12)):
        print(f"outside the pilot ({FIRST_NIGHT} to {PILOT_END:%Y-%m-%d}): nothing to send")
        return 0
    os.makedirs(OUT_DIR, exist_ok=True)
    marker = os.path.join(OUT_DIR, f"sent_{day.isoformat()}.marker")
    if os.path.exists(marker) and not (a.force or a.dry or a.test):
        print(f"already sent for {day} ({marker}); --force to resend")
        return 0
    subject, page, text, ctx = build(day, now, series=a.series, window=tuple(a.window.split("-")))
    if a.test:
        subject = "[TEST] " + subject
    out = os.path.join(OUT_DIR, f"report_{day.isoformat()}{'_' + a.series if a.series != SERIES else ''}.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    n, s = ctx["night"], ctx["since"]
    print(f"subject: {subject}".encode("ascii", "replace").decode("ascii"))
    print(f"night: {len(n['quoted'])} markets / {len(n['events'])} events, {len(n['fills'])} fills, "
          f"rew {n['rew']:.4f} pnl {n['pnl']:.4f}; since: {len(s['quoted'])} markets, "
          f"{len(s['fills'])} fills, rew {s['rew']:.4f} pnl {s['pnl']:.4f}; status {ctx['status']}; html {out}")
    if a.dry:
        return 0
    if not send(subject, page, text):
        return 1
    if not a.test:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(now.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
