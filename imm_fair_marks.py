#!/usr/bin/env python3
r"""imm_fair_marks.py -- every fair model's view, logged beside the market,
every 15 minutes (Jack 2026-11-26: "Yes, build it", to "Build the fair-model
scorecard: log the fair behind each quote, score every model daily?").

The bot quotes off 67 fair models, but nothing kept what they said: 30 of the
31 fair files in run-logs are overwritten on every refresh, and the orders log
records the price, not the fair behind it. So nobody could tell which models
beat the market. This keeps that history without touching the bot (whose file
is often carrying other sessions' live edits):

    python imm_fair_marks.py run [--dry] [--log FILE]   # "KL imm fair-marks", every 15 min
    python imm_fair_marks.py status

Each run:
  1. MARKS    every fair file in run-logs/incentive-mm whose markets carry a
              YES probability (markets -> ticker -> "p", or "fair": 15
              families and ~4,800 markets on 11/26; a new file of that shape
              joins by itself) is read beside the market's book: the bot's own
              latest external bid/ask from the tail of the cycle log for the
              ~180 series it is cycling, else Kalshi's top of book, read hourly
              in batches of 100. A mark goes to fair_marks_<UTC date>.jsonl
              when the fair moved MOVE_PP, the mid moved MOVE_C, or EVERY_S
              passed (EVERY_NO_BOOK_S without a book):
                 {ts, family, ticker, p, src_ts, bid, ask, book_ts, book_src}
  2. RAW      the other fair files (Vercel, Carbon Arc, OpenRouter, the
              temperature grids ...: shapes of their own) are archived raw, at
              most hourly and only when changed, to fair_raw_<UTC date>.jsonl.gz,
              so their history exists before anyone writes their extractor.
  3. RESULTS  a market that dropped out of its model's file has probably
              closed. Up to RESULT_BATCH of those a run are looked up through
              kalshi_reads (signed and paced: Kalshi throttles unsigned list
              reads), and each finalized yes/no goes to
              fair_results_<UTC date>.jsonl:
                 {ts, ticker, family, result, close_ts, status}
              The scorer (imm_fair_scorecard.py, the review's evidence section
              13) pairs it with the last mark before close.

State: fair_marks_state.json. IMM_FAIR_MARKS=0 turns it off. Read-only on the
bot and its files.
"""
from __future__ import annotations

import argparse
import csv
import glob
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import imm_review as rv  # noqa: E402

try:  # a task console is cp1252: never die on a glyph
    sys.stdout.reconfigure(errors="backslashreplace")
    sys.stderr.reconfigure(errors="backslashreplace")
except Exception:
    pass

MOVE_PP = 0.01            # a fair move of 1 percentage point is a new mark
MOVE_C = 1.0              # so is a 1c move in the market's mid
EVERY_S = 1800            # else one mark per 30 min for a market with a book
EVERY_NO_BOOK_S = 3600    # and one an hour for a market without one
BOOK_FRESH_S = 1200       # a cycle-log row older than 20 min is no book
TAIL_BYTES = 16_000_000   # how much of the cycle log's end one run reads
RAW_EVERY_S = 3600        # the raw archive: at most hourly per file
RAW_MAX_BYTES = 3_000_000
RESULT_BATCH = 300        # result lookups a run (three signed calls of 100)
RESULT_RETRY_S = 6 * 3600
GIVE_UP_DAYS = 14         # a market with no result after this is dropped
DONE_KEEP_DAYS = 30
STATE_NAME = "fair_marks_state.json"
SKIP_RX = re.compile(r"(_history|_state)\.json$")       # already a history; our own state


def _iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def state_path() -> str:
    return os.path.join(rv.STATUS_DIR, STATE_NAME)


def load_state() -> dict:
    st = rv._load_json(state_path(), {}) or {}
    for k in ("last", "cur", "lookup", "done", "raw"):
        st.setdefault(k, {})
    return st


def fair_files() -> List[str]:
    return [p for p in sorted(glob.glob(os.path.join(rv.STATUS_DIR, "*fair*.json")))
            if not SKIP_RX.search(os.path.basename(p))]


def family_of_file(path: str) -> str:
    """approval_fair.json -> approval; rain_fair_values.json -> rain."""
    return re.sub(r"_fair(_values)?$", "", os.path.basename(path)[:-len(".json")])


def extract(d) -> Dict[str, Tuple[float, object]]:
    """{ticker: (YES probability, its timestamp)} from a file of the standard
    shape (markets -> ticker -> "p" or "fair"); {} for any other shape.
    Entries flagged "err" or without a number are left out."""
    m = d.get("markets") if isinstance(d, dict) else None
    if not isinstance(m, dict):
        return {}
    default_ts = d.get("generated_at") or d.get("checked_ts") or d.get("fetched_at") or d.get("at")
    out: Dict[str, Tuple[float, object]] = {}
    for t, e in m.items():
        if not isinstance(e, dict) or e.get("err"):
            continue
        p = _num(e.get("p"))
        if p is None:
            p = _num(e.get("fair"))
        if p is None:
            continue
        out[str(t)] = (min(1.0, max(0.0, p)), e.get("fetched_at") or default_ts)
    return out


def _cycle_paths(now: float) -> List[str]:
    """Today's cycle log, and yesterday's too in the first minutes after 00:00 UTC."""
    today = _utc_day(now)
    paths = []
    if now - datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() < 2 * BOOK_FRESH_S:
        paths.append(os.path.join(rv.STATUS_DIR, f"cycle_log_{_utc_day(now - 86400)}.csv"))
    paths.append(os.path.join(rv.STATUS_DIR, f"cycle_log_{today}.csv"))
    return paths


def latest_books(now: float) -> Dict[str, Tuple[Optional[float], Optional[float], float]]:
    """{ticker: (ext_bid, ext_ask, ts)} for every market the bot saw in the
    last BOOK_FRESH_S, from the end of the cycle log (the bot appends one row
    per market per cycle)."""
    out: Dict[str, Tuple[Optional[float], Optional[float], float]] = {}
    ts_memo: Dict[str, Optional[float]] = {}
    for path in _cycle_paths(now):
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                header = fh.readline().decode("utf-8", "replace").strip().split(",")
                start = max(len(",".join(header)) + 1, size - TAIL_BYTES)
                fh.seek(start)
                data = fh.read()
        except OSError:
            continue
        try:
            i_ts, i_t, i_b, i_a = (header.index(c) for c in ("ts", "ticker", "ext_bid", "ext_ask"))
        except ValueError:
            continue
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]                 # the first piece may be a partial line
        for row in csv.reader(l.decode("utf-8", "replace") for l in lines if l):
            if len(row) != len(header):
                continue                      # a partial last line, being written
            s = row[i_ts]
            if s not in ts_memo:
                ts_memo[s] = rv._ts_of(s)
            ts = ts_memo[s]
            if ts is None or now - ts > BOOK_FRESH_S:
                continue
            out[row[i_t]] = (_num(row[i_b]), _num(row[i_a]), ts)
    return out


def _append(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")


def _append_gz(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    with gzip.open(path, "ab") as fh:         # one gzip member per run: readers see one stream
        for r in rows:
            fh.write((json.dumps(r, separators=(",", ":"), default=str) + "\n").encode("utf-8"))


def _cents(m: dict, key: str) -> Optional[float]:
    """A YES price in cents from a /markets row (the *_dollars strings; the
    integer field when an older payload carries it)."""
    v = _num(m.get(key + "_dollars"))
    if v is not None:
        return round(v * 100.0, 2)
    return _num(m.get(key))


def kalshi_markets(tickers: List[str]) -> Dict[str, dict]:
    """{ticker: {"bid", "ask", "status", "result", "close_ts", "type"}}, 100
    tickers a call, signed through kalshi_reads (Kalshi throttles unsigned
    list reads; signed reads are paced). An empty side is None: a 0c bid or a
    100c ask is no order at all."""
    from kalshi_reads import kalshi_get
    out: Dict[str, dict] = {}
    for i in range(0, len(tickers), 100):
        js = kalshi_get("/markets", {"tickers": ",".join(tickers[i:i + 100]), "limit": 100})
        for m in (js or {}).get("markets") or []:
            bid, ask = _cents(m, "yes_bid"), _cents(m, "yes_ask")
            out[m.get("ticker")] = {
                "bid": bid if bid and bid > 0 else None, "ask": ask if ask is not None and ask < 100 else None,
                "status": m.get("status") or "", "result": m.get("result") or "",
                "close_ts": rv._ts_of(m.get("close_time") or m.get("expiration_time")),
                "type": m.get("market_type") or ""}
    return out


QUOTES_EVERY_S = 3600     # markets without a cycle-log book: Kalshi's top of book, hourly
RESULT_STATUSES = ("determined", "finalized", "settled")


def run(dry: bool = False, now: Optional[float] = None,
        markets_fn: Optional[Callable[[List[str]], Dict[str, dict]]] = None) -> dict:
    now = now or time.time()
    t0 = time.time()
    out = {"at": _iso(now), "dry": dry, "files": 0, "families": {}, "raw_files": [], "marks": 0,
           "markets": 0, "books": 0, "api_books": 0, "reads": 0, "results": 0, "given_up": 0, "errors": []}
    if os.environ.get("IMM_FAIR_MARKS", "1") == "0":
        out["off"] = True
        return out
    st = load_state()
    last, cur, look, done, raw_st = st["last"], st["cur"], st["lookup"], st["done"], st["raw"]
    books = latest_books(now)
    out["books"] = len(books)
    marks: List[dict] = []
    raws: List[dict] = []
    fair: Dict[str, Tuple[str, float, object]] = {}       # ticker -> (family, p, its timestamp)
    for path in fair_files():
        fam = family_of_file(path)
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            d = json.loads(data.decode("utf-8"))
        except (OSError, ValueError) as e:
            out["errors"].append(f"{os.path.basename(path)}: {type(e).__name__}: {e}"[:200])
            continue
        out["files"] += 1
        got = extract(d)
        if not got:
            sha = hashlib.sha256(data).hexdigest()[:16]
            prev = raw_st.get(fam) or {}
            if prev.get("sha") != sha and now - _num(prev.get("ts") or 0) >= RAW_EVERY_S \
                    and len(data) <= RAW_MAX_BYTES:
                raws.append({"ts": _iso(now), "family": fam, "file": os.path.basename(path), "sha": sha,
                             "content": data.decode("utf-8", "replace")})
                raw_st[fam] = {"sha": sha, "ts": now}
                out["raw_files"].append(fam)
            continue
        out["families"][fam] = len(got)
        for t, (p, src) in got.items():
            fair[t] = (fam, p, src)
    # Kalshi reads, one batched call per 100: hourly top of book for the fair
    # markets the bot is not cycling, and result lookups for the markets that
    # dropped out of their model's file
    for t in [t for t, c in cur.items() if t not in fair and now - c[0] > GIVE_UP_DAYS * 86400]:
        cur.pop(t, None)
        look.pop(t, None)
        out["given_up"] += 1
    need_q = (sorted(t for t in fair if t not in books)
              if now - _num(st.get("quotes_at") or 0) >= QUOTES_EVERY_S else [])
    cands = sorted((t for t in cur if t not in fair and t not in done
                    and now - _num(look.get(t) or 0) >= RESULT_RETRY_S),
                   key=lambda t: _num(look.get(t) or 0))[:RESULT_BATCH]
    api: Dict[str, dict] = {}
    to_read = list(dict.fromkeys(need_q + cands))
    if to_read and not dry:
        try:
            api = (markets_fn or kalshi_markets)(to_read)
            if need_q:
                st["quotes_at"] = round(now, 1)
        except Exception as e:  # marks still land; the next run retries
            out["errors"].append(f"kalshi reads: {type(e).__name__}: {e}"[:200])
        out["reads"] = len(to_read)
    for t, (fam, p, src) in fair.items():
        if t in books:
            (bid, ask, bts), src_b = books[t], "cycle"
        elif t in api:
            bid, ask, bts, src_b = api[t].get("bid"), api[t].get("ask"), now, "api"
            out["api_books"] += 1
        else:
            bid = ask = bts = src_b = None
        mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
        cur[t] = [round(now, 1), round(p, 5), fam]
        prev = last.get(t)
        due = (prev is None or abs(p - prev[1]) >= MOVE_PP
               or (mid is not None and (prev[2] is None or abs(mid - prev[2]) >= MOVE_C))
               or now - prev[0] >= (EVERY_S if mid is not None else EVERY_NO_BOOK_S))
        if due:
            marks.append({"ts": _iso(now), "family": fam, "ticker": t, "p": round(p, 5), "src_ts": src,
                          "bid": bid, "ask": ask, "book_ts": _iso(bts), "book_src": src_b})
            last[t] = [round(now, 1), round(p, 5), mid]
    out["marks"], out["markets"] = len(marks), len(fair)
    # results: any market read here that has settled yes/no (a model may keep
    # publishing a market after it closed, so the quote reads count too)
    results: List[dict] = []
    for t, r in api.items():
        if t in done or r.get("type") not in ("", "binary"):
            continue
        if r.get("result") in ("yes", "no") and str(r.get("status")) in RESULT_STATUSES:
            fam = fair[t][0] if t in fair else ((cur.get(t) or [None, None, None])[2] or "")
            results.append({"ts": _iso(now), "ticker": t, "family": fam, "result": r["result"],
                            "close_ts": _iso(r.get("close_ts")), "status": r.get("status")})
            done[t] = round(now, 1)
            cur.pop(t, None)
            look.pop(t, None)
    for t in cands:
        if t not in done and not dry:
            look[t] = round(now, 1)
    out["results"] = len(results)
    for t in [t for t, v in done.items() if now - _num(v or 0) > DONE_KEEP_DAYS * 86400]:
        done.pop(t, None)
    for t in [t for t, v in last.items() if t not in fair and now - v[0] > 3 * 86400]:
        last.pop(t, None)
    out["secs"] = round(time.time() - t0, 2)
    if not dry:
        day = _utc_day(now)
        _append(os.path.join(rv.STATUS_DIR, f"fair_marks_{day}.jsonl"), marks)
        _append(os.path.join(rv.STATUS_DIR, f"fair_results_{day}.jsonl"), results)
        _append_gz(os.path.join(rv.STATUS_DIR, f"fair_raw_{day}.jsonl.gz"), raws)
        st["run"] = {k: out[k] for k in ("at", "files", "marks", "markets", "books", "api_books", "reads",
                                          "results", "given_up", "errors", "secs")}
        st["run"]["families"] = out["families"]
        rv._write_json(state_path(), st)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry", action="store_true")
    r.add_argument("--log", help="append a one-line summary here (pythonw has no console)")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        print(json.dumps(load_state().get("run") or {}, indent=1))
        return 0
    try:
        out = run(dry=a.dry)
    except Exception as e:
        # pythonw has no console, so a traceback would vanish: it goes to the
        # task log as a FAILED line, which the review's task-log scan reports
        import traceback
        line = json.dumps({"at": _iso(time.time()), "status": "FAILED",
                           "error": f"{type(e).__name__}: {e}"[:300],
                           "trace": traceback.format_exc()[-2000:]})
        print(line)
        _log_line(a.log, line)
        return 2
    # a fair file caught mid-rewrite is noted, not a failed run: fail only when
    # nothing could be read at all (Task Scheduler shows the exit code)
    ok = bool(out.get("off") or out["files"])
    row = {k: v for k, v in out.items() if k != "families"}
    if not ok:
        row["status"] = "FAILED: no fair file could be read"
    line = json.dumps(row, default=str)
    print(line)
    _log_line(a.log, line)
    return 0 if ok else 1


def _log_line(path: Optional[str], line: str) -> None:
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
