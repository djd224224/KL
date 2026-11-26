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
  4. BOT VIEW (2026-11-26, Jack: "Do this task here", the 15 raw-shaped
              files) ten files price a distribution or an event, not each
              market: Carbon Arc's mu/sigma per series, OpenRouter's per event,
              Vercel's error sample, the temperature and YouTube writers'
              events. The bot turns them into a market's YES probability with
              its own functions, so the CLI imports the bot (as
              imm_model_coverage does) and asks those functions -- for the
              markets in the bot's newest selection snapshot and cycle log, or
              the ones the file lists itself. Marks as in 1. See BOT_VIEW.

State: fair_marks_state.json. IMM_FAIR_MARKS=0 turns it off;
IMM_FAIR_MARKS_BOT_VIEW=0 turns off step 4 alone. Read-only on the bot and its
files: the bot's module is imported into this process, never the running bot,
and every global a pass sets is put back.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import glob
import gzip
import hashlib
import io
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

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


def is_standard(d) -> bool:
    """The standard shape (markets -> ticker -> {...}) even with nothing priced
    right now -- ERCOT past its cutoff, Trump mentions between speeches,
    Pokemon unmapped: its family has 0 markets, it is not a raw file."""
    m = d.get("markets") if isinstance(d, dict) else None
    return isinstance(m, dict) and all(isinstance(v, dict) for v in m.values())


# ---------------------------------------------------------------------------
# BOT VIEW: the fair files that need the bot's own conversion
# ---------------------------------------------------------------------------
# A market is priced when the bot's fair function returns a fair, or its gate
# passes with no book given (the bot would quote against that fair). So a
# stale read, a "decided" strike, a live temperature period or a complete
# OpenRouter window gets no mark, as the bot uses none. Re-deriving the math
# here would drift from the bot; asking its functions cannot.
#   - Vercel is scored on the MODEL (vercel_fair_p), not the blend with the
#     book's mid the bot leads with since 11/26: the blend is part market.
#   - Not here: datacenter (a count gate, no fair), gasbuddy (retired), Carbon
#     Arc's array shadow, and the per-city rain dailies (the station each
#     one's rules name is learned by the running bot; the KXRAIN dailies are
#     priced). They stay in the raw archive.
UNIVERSE_TAIL_BYTES = 12_000_000   # the newest selection snapshot (~4,300 rows, ~6 MB on 11/26)
UNIVERSE_WINDOW_S = 600            # rows within 10 min of the newest are that snapshot
_BOT = None


@contextlib.contextmanager
def _quiet():
    """The bot's log() prints; keep its lines out of ours (and pythonw has
    no console)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


def import_bot():
    """The bot's module, imported the way imm_model_coverage does it:
    imm_quote_gaps first, which mirrors the launcher's env, so the TTLs and
    switches are the live ones. Only the CLI (the task) calls this; run()
    never imports the bot unless handed this, and tests hand it a fake (an
    import would change the env the rest of a test run sees)."""
    global _BOT
    if _BOT is None:
        with _quiet():
            import imm_quote_gaps  # noqa: F401
            import incentive_mm
        _BOT = incentive_mm
    return _BOT


def universe_tickers() -> Set[str]:
    """The tickers of the bot's newest hourly selection snapshot (every
    candidate it weighed, ~4,300 on 11/26); empty when there is none."""
    files = sorted(glob.glob(os.path.join(rv.STATUS_DIR, "selection_snapshot_*.jsonl")))
    for path in reversed(files[-2:]):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        n = UNIVERSE_TAIL_BYTES
        while True:
            try:
                with open(path, "rb") as fh:
                    fh.seek(max(0, size - n))
                    if size > n:
                        fh.readline()                     # a partial first line
                    data = fh.read()
            except OSError:
                break
            rows: List[Tuple[float, str]] = []
            for line in data.splitlines():
                try:
                    r = json.loads(line)
                    ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00")).timestamp()
                    rows.append((ts, str(r["ticker"])))
                except (ValueError, KeyError, TypeError):
                    continue
            if not rows:
                break
            newest = max(ts for ts, _ in rows)
            if size > n and rows[0][0] >= newest - UNIVERSE_WINDOW_S:
                n *= 2                                    # the snapshot began before this chunk
                continue
            return {t for ts, t in rows if ts >= newest - UNIVERSE_WINDOW_S}
    return set()


def _bv_carbon_arc(imm, cands: Set[str], now: float) -> dict:
    """ca_fair_p: P(first print > K) at the read's sigma, for a fresh read of
    the market's measurement month."""
    ents = imm._ca_fair_state["entries"]
    out = {}
    for t in cands:
        e = ents.get(imm.series_of(t))
        if e is not None:
            p = imm.ca_fair_p(t, now)
            if p is not None:
                out[t] = (p, e.get("ts"))
    return out


def _bv_openrouter(imm, cands: Set[str], now: float) -> dict:
    """or_fair_c (YES cents): P(the window's total > K) while the gate reads
    it (fresh, the window not complete)."""
    ents = imm._or_fair_state["entries"]
    out = {}
    for t in cands:
        e = ents.get(t.rsplit("-", 1)[0])
        if e is not None:
            c = imm.or_fair_c(t, now)
            if c is not None:
                out[t] = (c / 100.0, e.get("ts"))
    return out


def _bv_share(imm, cands: Set[str], now: float) -> dict:
    """share_p_yes behind share_gate_reason: P(the share rounds above K) x
    P(the author is named)."""
    ents = imm._share_fair_state["entries"]
    out = {}
    for t in cands:
        e = ents.get(t.rsplit("-", 1)[0])
        if e is None or imm.share_gate_reason(t, now, None, None)[0]:
            continue
        m = imm._SHARE_STRIKE_RE.search(t)
        out[t] = (imm.share_p_yes(float(m.group(1)), e["mu"], e["sigma"], e["p_ident"]), e.get("ts"))
    return out


def _bv_vercel(imm, cands: Set[str], now: float) -> dict:
    """vercel_fair_p (the model) where vercel_gate_reason passes: a fresh
    read, before the measured day, not decided."""
    ents = imm._vercel_state["entries"]
    out = {}
    for t in cands:
        ev, _, ks = t.rpartition("-")
        e = ents.get(ev)
        if e is None or imm.vercel_gate_reason(t, now, None, None)[0]:
            continue
        m = imm._VERCEL_STRIKE_RE.match(ks)
        out[t] = (imm.vercel_fair_p(e, float(f"{m.group(1)}.{m.group(2) or 0}")), e.get("ts"))
    return out


def _bv_rain(imm, cands: Set[str], now: float) -> dict:
    """rain_fair_p for the KXRAIN dailies (and any per-city daily whose
    station the bot module knows)."""
    out = {}
    for t in cands:
        s = imm.series_of(t)
        if s == imm.RAIN_FAIR_SERIES or imm.rain_daily_city_series(s):
            p = imm.rain_fair_p(t, now)
            if p is not None:
                out[t] = (p, None)
    return out


def _bv_temp(state_name: str, gate_name: str) -> Callable:
    """A temperature writer's own markets where its gate passes with no
    book (fresh, not skipped; for a period, not live, decided or near)."""
    def price(imm, cands: Set[str], now: float) -> dict:
        out = {}
        for e in list(getattr(imm, state_name)["events"].values()):
            mk = e.get("markets") or {}
            for t in list(mk):
                if not getattr(imm, gate_name)(t, now, None, None)[0]:
                    out[t] = (float(mk[t]), e.get("ts"))
        return out
    return price


def _bv_yt_top1(imm, cands: Set[str], now: float) -> dict:
    """The #1 pilot's leader at its fair p, where yt1_gate_reason passes."""
    out = {}
    for ev, e in imm._yt1_state["entries"].items():
        t = f"{ev}-{e['leader']}"
        if not imm.yt1_gate_reason(t, now)[0]:
            out[t] = (float(e["p"]), e.get("ts"))
    return out


def _bv_yt_weekly(imm, cands: Set[str], now: float) -> dict:
    """Each weekly strike's p where ytw_gate_reason passes (not held for a
    print, not decided)."""
    out = {}
    for ev, e in imm._ytw_state["entries"].items():
        for key, s in (e.get("strikes") or {}).items():
            t = f"{ev}-{key}"
            if not imm.ytw_gate_reason(t, now, None, None)[0]:
                out[t] = (float(s["p"]), e.get("ts"))
    return out


# family: (the bot's path constant, its loaded state, its loader, the pricer)
BOT_VIEW: Dict[str, Tuple[str, str, str, Callable]] = {
    "carbon_arc": ("CA_FAIR_FILE", "_ca_fair_state", "load_ca_fair", _bv_carbon_arc),
    "openrouter": ("OR_FAIR_FILE", "_or_fair_state", "load_or_fair", _bv_openrouter),
    "openrouter_share": ("SHARE_FAIR_FILE", "_share_fair_state", "load_share_fair", _bv_share),
    "vercel": ("VERCEL_FAIR_FILE", "_vercel_state", "load_vercel_fair", _bv_vercel),
    "rain": ("RAIN_FAIR_FILE", "_rain_fair_state", "load_rain_fair", _bv_rain),
    "hight": ("HIGHT_FAIR_FILE", "_hight_state", "load_hight_fair", _bv_temp("_hight_state", "hight_gate")),
    "lowt": ("LOWT_FAIR_FILE", "_lowt_state", "load_lowt_fair", _bv_temp("_lowt_state", "lowt_gate")),
    "lowt_period": ("LOWT_PERIOD_FAIR_FILE", "_lowt_period_state", "load_lowt_period_fair",
                    _bv_temp("_lowt_period_state", "lowt_period_gate")),
    "yt_top1": ("YT1_FAIR_FILE", "_yt1_state", "load_yt1_fair", _bv_yt_top1),
    "yt_weekly": ("YTW_FAIR_FILE", "_ytw_state", "load_ytw_fair", _bv_yt_weekly),
}


def bot_view(files: Dict[str, str], cands: Set[str], now: float, imm,
             errors: List[str]) -> Dict[str, Dict[str, Tuple[float, object]]]:
    """{family: {ticker: (YES probability, its timestamp)}} from the bot's own
    functions, each family's file loaded from `files` (the path this run
    read). The bot's globals each pass sets -- the file path, the loaded
    state -- are put back after it. A failure is a "bot view <family>
    FAILED" error, which the review's task-log scan reports."""
    out: Dict[str, Dict[str, Tuple[float, object]]] = {}
    for fam, path in sorted(files.items()):
        path_attr, state_name, loader, price = BOT_VIEW[fam]
        try:
            saved_path = getattr(imm, path_attr)
            state = getattr(imm, state_name)
            saved = dict(state)
        except AttributeError as e:
            errors.append(f"bot view {fam} FAILED: {type(e).__name__}: {e}"[:200])
            continue
        try:
            setattr(imm, path_attr, path)
            state["mtime"] = 0.0                      # force the hot-reload
            for k in ("entries", "events", "fair", "markets", "moved_at"):
                if isinstance(state.get(k), dict):
                    state[k] = {}                     # a first load: no refresh holds
            with _quiet() as buf:
                getattr(imm, loader)()
                got = price(imm, cands, now)
            for line in buf.getvalue().splitlines():  # the bot's own warnings ("] ! ...")
                if "] !" in line:
                    errors.append(f"bot view {fam}: {line.split('] ', 1)[-1]}"[:200])
            rows: Dict[str, Tuple[float, object]] = {}
            for t, (p, src) in got.items():
                p = _num(p)
                if p is not None and math.isfinite(p):
                    rows[t] = (min(1.0, max(0.0, p)), _iso(src) if isinstance(src, (int, float)) else src)
            out[fam] = rows
        except Exception as e:
            errors.append(f"bot view {fam} FAILED: {type(e).__name__}: {e}"[:200])
        finally:
            setattr(imm, path_attr, saved_path)
            state.clear()
            state.update(saved)
    return out


def run(dry: bool = False, now: Optional[float] = None,
        markets_fn: Optional[Callable[[List[str]], Dict[str, dict]]] = None,
        bot: Optional[Callable[[], object]] = None) -> dict:
    """One pass. `bot` returns the bot's module for the BOT VIEW (the CLI
    passes import_bot); without it those families stay raw only."""
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
    bot_files: Dict[str, str] = {}                        # BOT VIEW family -> the file read
    file_ts: Dict[str, object] = {}
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
            if is_standard(d):
                out["families"][fam] = 0          # nothing priced right now; not a raw file
                continue
            if fam in BOT_VIEW:
                bot_files[fam] = path
                if isinstance(d, dict):
                    file_ts[fam] = (d.get("generated_at") or d.get("updated_at")
                                    or d.get("fetched_at") or d.get("at"))
            # the whole file goes to the raw archive too, BOT VIEW or not: it
            # keeps what the marks drop (mu, sigma, the error sample)
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
    if bot_files and bot is not None and os.environ.get("IMM_FAIR_MARKS_BOT_VIEW", "1") != "0":
        try:
            imm = bot()
        except Exception as e:  # the standard families' marks still land
            out["errors"].append(f"bot view FAILED: {type(e).__name__}: {e}"[:200])
            imm = None
        if imm is not None:
            cands = set(books) | universe_tickers()
            for fam, got in bot_view(bot_files, cands, now, imm, out["errors"]).items():
                out["families"][fam] = len(got)
                for t, (p, src) in got.items():
                    fair.setdefault(t, (fam, p, src or file_ts.get(fam)))
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
        out = run(dry=a.dry, bot=import_bot)
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
