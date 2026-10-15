#!/usr/bin/env python3
"""One-shot, resumable history pull for the hourly-temp shadow test
(TEMP_SHADOW.md). READ-ONLY: unsigned public GETs only, <= 1 request/s
(the always-on collector runs beside it at ~0.7 req/s); the spacing
grows to 3 s while the station feed's landing window ([13, 50] s of each
minute) is open (unsigned requests share one per-IP bucket, ~2 req/s).

Writes under KL-data\\temp-shadow\\backfill\\ (TEMP_SHADOW_DATA):

  wx/detailed_<from_ms>.json   raw GET /live_data/weather/miami?detailed=true
                               &from=<ms>&to=<ms> responses, 6-hour chunks
                               (to = from + 6h - 1 min: inclusive bounds)
                               from 2026-09-01T00:00Z to now. A chunk is
                               skipped once fetched >= 15 min after its end
                               (complete); the partial last chunk is refetched.
  settled/<EVENT>.json         settled KXTEMPMIAH events of the last 21 days in
                               the SAME shape as the collector's
                               settled/<EVENT>.json: {"event_ticker",
                               "fetched_rx", "complete", "markets" (REST, with
                               result / expiration_value), "trades" {ticker:
                               [REST trades]}, "index" (live_data/events
                               timeseries rows), "index_details", "programs",
                               "source"}. Oct 7-13 trades and index rows are
                               converted from Agent B's study pull
                               (backfill/study/agentB_data/) instead of
                               refetched, and events the live collector
                               already pulled (settled/) are copied;
                               programs for Aug 15 - Sep 30 come
                               from Agent A's prog_temp.json (window + reward
                               only, marked "_source"). Existing files are
                               skipped.
  calibrations.json            GET /live_data/weather/miami/calibrations
  backfill.log                 progress log

CLI: python temp_shadow_backfill.py [--wx] [--settled] [--calibrations]
                                    [--since ISO] [--days N] [--gap SECS]
     (no section flags = all three)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Dict, List, Optional

import temp_shadow_common as C
import temp_shadow_collector as TC

BF = os.path.join(C.DATA_ROOT, "backfill")
STUDY_B = os.path.join(BF, "study", "agentB_data")
STUDY_A = os.path.join(BF, "study", "agentA")
LOG = os.path.join(BF, "backfill.log")
CHUNK_MS = 6 * 3600 * 1000
WX_SINCE = "2026-09-01T00:00:00Z"


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + "Z " + msg
    print(line, flush=True)
    try:
        os.makedirs(BF, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


class Getter:
    """Throttled unsigned GETs: >= gap seconds between request starts, 429
    backoff 2..60 s, transient errors retried."""

    def __init__(self, gap: float = 1.05):
        self.http = TC.Http(None, timeout=60.0)
        self.gap = gap
        self.last = 0.0
        self.rate = TC.RateState(sign_after=10 ** 9)
        self.n = 0
        self.n429 = 0
        self.errors: List[str] = []

    def get(self, path: str, params: Optional[dict] = None, tries: int = 6) -> TC.Resp:
        r = TC.Resp(0, None, 0.0, "not attempted")
        for i in range(tries):
            # >= gap s apart, >= 3 s while the station feed's landing window
            # ([WX_LO, WX_HI] s of the minute) is open: the live collector's wx
            # poller shares this IP's unsigned budget (~2 req/s sustained)
            while True:
                now = time.time()
                gap = TC.rest_gap(now, self.gap, max(3.0, self.gap))
                wait = max(self.last + gap, self.rate.backoff_until) - now
                if wait <= 0:
                    break
                time.sleep(min(wait, 0.25))
            self.last = time.time()
            r = self.http.get(path, params)
            self.n += 1
            if r.status == 429:
                self.n429 += 1
                w = self.rate.on_429(time.time())
                log(f"429 on {path}; backing off {w:.0f}s")
                continue
            if r.ok:
                self.rate.on_ok(time.time())
                return r
            if r.status in (400, 401, 403, 404):
                return r
            time.sleep(min(2 ** i, 30))
        self.errors.append(f"{path} {params}: {r.error[:200]}")
        return r

    def paginate(self, path: str, params: dict, key: str, max_pages: int = 200) -> List[dict]:
        out: List[dict] = []
        cursor = None
        for _ in range(max_pages):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            r = self.get(path, p)
            if not r.ok:
                raise RuntimeError(f"GET {path} {p}: {r.error[:200]}")
            batch = r.body.get(key) or []
            out.extend(batch)
            cursor = r.body.get("cursor")
            if not cursor or not batch:
                break
        return out


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# (a) station rows
# --------------------------------------------------------------------------

def chunk_complete(path: str, chunk_end_ms: int) -> bool:
    try:
        return os.path.getmtime(path) >= chunk_end_ms / 1000.0 + 900
    except OSError:
        return False


def pull_wx(g: Getter, since_iso: str, city: str = "miami") -> dict:
    d = os.path.join(BF, "wx")
    os.makedirs(d, exist_ok=True)
    a = int(C.parse_iso(since_iso) * 1000)
    now_ms = int(time.time() * 1000)
    t = a
    fetched = skipped = empty = 0
    errors: List[str] = []
    while t < now_ms:
        end = t + CHUNK_MS
        p = os.path.join(d, f"detailed_{t}.json")
        if os.path.exists(p) and chunk_complete(p, end):
            skipped += 1
            t = end
            continue
        to = min(end - 60000, now_ms)
        r = g.get(f"/live_data/weather/{city}", {"detailed": "true", "from": t, "to": to})
        if r.ok and isinstance(r.body, dict):
            write_json(p, r.body)
            n = len(r.body.get("timeseries") or [])
            fetched += 1
            if n == 0:
                empty += 1
            log(f"wx {C.iso(t / 1000)} .. {C.iso(to / 1000)}: {n} rows")
        else:
            errors.append(f"{C.iso(t / 1000)}: {r.error[:160]}")
            log(f"wx {C.iso(t / 1000)}: ERROR {r.error[:160]}")
        t = end
    return {"fetched": fetched, "skipped_complete": skipped, "empty": empty, "errors": errors}


def wx_coverage(city_dir: Optional[str] = None) -> dict:
    """Minutes present across all chunks: first/last, total, missing minutes,
    longest hole."""
    d = city_dir or os.path.join(BF, "wx")
    ts = set()
    status = {}
    for name in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not name.startswith("detailed_") or not name.endswith(".json"):
            continue
        body = C.read_json(os.path.join(d, name), {}) or {}
        for row in body.get("timeseries") or []:
            if row.get("t") is not None:
                ts.add(int(row["t"]))
                status[row.get("status")] = status.get(row.get("status"), 0) + 1
    if not ts:
        return {"minutes": 0}
    lo, hi = min(ts), max(ts)
    expected = (hi - lo) // 60000 + 1
    srt = sorted(ts)
    holes = [(b - a) // 60000 - 1 for a, b in zip(srt, srt[1:]) if b - a > 60000]
    longest = 0
    longest_at = None
    for a, b in zip(srt, srt[1:]):
        if (b - a) // 60000 - 1 > longest:
            longest, longest_at = (b - a) // 60000 - 1, C.iso(a / 1000 + 60)
    return {"first": C.iso(lo / 1000), "last": C.iso(hi / 1000), "minutes": len(ts),
            "missing_minutes": expected - len(ts), "holes": len(holes),
            "longest_hole_min": longest, "longest_hole_at": longest_at, "status": status}


# --------------------------------------------------------------------------
# (b) settled events
# --------------------------------------------------------------------------

def load_agent_a_programs() -> Dict[str, List[dict]]:
    """event -> [program-like dicts] from Agent A's prog_temp.json
    ({ticker: {start, end, paid, reward, periods}})."""
    raw = C.read_json(os.path.join(STUDY_A, "prog_temp.json"), {}) or {}
    out: Dict[str, List[dict]] = {}
    for tk, v in raw.items():
        if not isinstance(v, dict):
            continue
        for per in v.get("periods") or [[v.get("start"), v.get("end"), v.get("paid")]]:
            try:
                start, end, paid = per[0], per[1], per[2]
            except (TypeError, IndexError):
                continue
            out.setdefault(C.event_of(tk), []).append(
                {"market_ticker": tk, "start_date": start, "end_date": end,
                 "period_reward": v.get("reward"), "paid_out": paid,
                 "incentive_type": "liquidity",
                 "_source": "study/agentA/prog_temp.json (window + reward only)"})
    return out


def load_agent_b_programs() -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for name in ("incentive_programs_KXTEMPMIAH_1410.json",
                 "incentive_programs_KXTEMPMIAH_1411.json"):
        for p in C.read_json(os.path.join(STUDY_B, name), []) or []:
            if isinstance(p, dict) and p.get("market_ticker"):
                out.setdefault(C.event_of(p["market_ticker"]), []).append(p)
    return out


def pull_settled(g: Getter, days: float, series: str = "KXTEMPMIAH") -> dict:
    d = os.path.join(BF, "settled")
    os.makedirs(d, exist_ok=True)
    now = time.time()
    markets = g.paginate("/markets", {"series_ticker": series, "status": "settled",
                                      "min_close_ts": int(now - days * 86400),
                                      "max_close_ts": int(now), "limit": 1000}, "markets")
    by_ev: Dict[str, List[dict]] = {}
    for m in markets:
        by_ev.setdefault(m.get("event_ticker") or C.event_of(m["ticker"]), []).append(m)
    log(f"settled: {len(markets)} markets in {len(by_ev)} events over the last {days:g} days")
    progs_a = load_agent_a_programs()
    progs_b = load_agent_b_programs()
    stats = {"events": len(by_ev), "written": 0, "skipped_existing": 0,
             "not_all_results": [], "from_agentB": 0, "trade_requests": 0, "errors": [],
             "first_event": min(by_ev) if by_ev else None,
             "last_event": max(by_ev) if by_ev else None}
    for i, evt in enumerate(sorted(by_ev, key=lambda e: (C.parse_event(e) or ("", 0))[1])):
        out = os.path.join(d, f"{evt}.json")
        if os.path.exists(out):
            stats["skipped_existing"] += 1
            continue
        mk = sorted(by_ev[evt], key=lambda m: m.get("ticker", ""))
        if not TC.all_results(mk):
            stats["not_all_results"].append(evt)
            continue
        live_file = C.read_json(C.settled_file(evt))
        if isinstance(live_file, dict) and live_file.get("complete") and live_file.get("trades"):
            # the live collector already pulled it (same shape): copy, no requests
            obj = dict(live_file)
            obj["source"] = {"markets": "collector", "trades": "collector",
                             "index": "collector", "programs": "collector"}
            write_json(out, obj)
            stats["written"] += 1
            stats["from_collector"] = stats.get("from_collector", 0) + 1
            continue
        try:
            src = {"markets": "rest"}
            b_trades = C.read_json(os.path.join(STUDY_B, "trades", f"{evt}.json"))
            trades: Dict[str, List[dict]] = {}
            refetched = []
            for m in mk:
                tk = m["ticker"]
                vol = C.fp(m.get("volume_fp") if m.get("volume_fp") is not None
                           else m.get("volume"))
                if isinstance(b_trades, dict) and tk in b_trades:
                    trades[tk] = b_trades[tk]
                    continue
                if vol <= 0:
                    trades[tk] = []
                    continue
                trades[tk] = g.paginate("/markets/trades", {"ticker": tk, "limit": 1000},
                                        "trades")
                stats["trade_requests"] += 1
                refetched.append(tk)
            src["trades"] = ("agentB" if isinstance(b_trades, dict) and not refetched
                             else "rest" if not isinstance(b_trades, dict) else "agentB+rest")
            b_live = C.read_json(os.path.join(STUDY_B, "live", f"{evt}.json"))
            if isinstance(b_live, dict) and (b_live.get("live_data") or {}).get("details"):
                details = b_live["live_data"]["details"]
                src["index"] = "agentB"
            else:
                r = g.get(f"/live_data/events/{evt}")
                if not r.ok:
                    raise RuntimeError(f"live_data/events/{evt}: {r.error[:160]}")
                details = (r.body.get("live_data") or {}).get("details") or {}
                src["index"] = "rest"
            if src["trades"].startswith("agentB") and src["index"] == "agentB":
                stats["from_agentB"] += 1
            progs = progs_b.get(evt) or progs_a.get(evt) or []
            src["programs"] = ("agentB" if evt in progs_b else "agentA" if evt in progs_a
                               else "none")
            obj = {"event_ticker": evt, "fetched_rx": time.time(), "complete": True,
                   "markets": mk, "trades": trades,
                   "index": details.get("timeseries") or [],
                   "index_details": {k: v for k, v in details.items() if k != "timeseries"},
                   "programs": sorted(progs, key=lambda p: (p.get("market_ticker", ""),
                                                            str(p.get("start_date")))),
                   "source": src}
            write_json(out, obj)
            stats["written"] += 1
            if stats["written"] % 10 == 1:
                log(f"settled {evt}: {sum(len(v) for v in trades.values())} trades, "
                    f"{len(obj['index'])} index rows, src {src} ({i + 1}/{len(by_ev)}; "
                    f"{g.n} requests so far)")
        except Exception as e:
            stats["errors"].append(f"{evt}: {type(e).__name__}: {str(e)[:160]}")
            log(f"settled {evt}: ERROR {type(e).__name__}: {e}")
    return stats


# --------------------------------------------------------------------------
# (c) calibrations
# --------------------------------------------------------------------------

def pull_calibrations(g: Getter, city: str = "miami") -> dict:
    r = g.get(f"/live_data/weather/{city}/calibrations")
    if not r.ok:
        return {"error": r.error[:200]}
    write_json(os.path.join(BF, "calibrations.json"), r.body)
    cals = (r.body or {}).get("calibrations") or []
    return {"calibrations": len(cals),
            "latest": cals[-1].get("config_version") if cals else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="hourly-temp shadow backfill")
    ap.add_argument("--wx", action="store_true")
    ap.add_argument("--settled", action="store_true")
    ap.add_argument("--calibrations", action="store_true")
    ap.add_argument("--since", default=WX_SINCE, help="wx start (ISO, default %(default)s)")
    ap.add_argument("--days", type=float, default=21.0, help="settled lookback (days)")
    ap.add_argument("--gap", type=float, default=1.05, help="seconds between requests (>= 1)")
    args = ap.parse_args(argv)
    every = not (args.wx or args.settled or args.calibrations)
    g = Getter(gap=max(1.0, args.gap))
    summary = {}
    t0 = time.time()
    if every or args.calibrations:
        summary["calibrations"] = pull_calibrations(g)
        log(f"calibrations: {summary['calibrations']}")
    if every or args.wx:
        summary["wx"] = pull_wx(g, args.since)
        summary["wx_coverage"] = wx_coverage()
        log(f"wx: {summary['wx']} coverage {summary['wx_coverage']}")
    if every or args.settled:
        summary["settled"] = pull_settled(g, args.days)
        s = dict(summary["settled"])
        s["not_all_results"] = len(s["not_all_results"])
        log(f"settled: {s}")
    summary["requests"] = g.n
    summary["n429"] = g.n429
    summary["elapsed_s"] = round(time.time() - t0, 1)
    summary["request_errors"] = g.errors[-20:]
    log("done: " + json.dumps({k: v for k, v in summary.items()
                               if k not in ("settled",)}, default=str)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
