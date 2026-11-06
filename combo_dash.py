#!/usr/bin/env python3
r"""combo_dash.py -- a small LOCAL dashboard for the combo maker pilot (Jack
2026-11-05: "build a simple dash i can use to track parlays").

    python combo_dash.py              # http://127.0.0.1:8772, refreshes every 15 s
    python combo_dash.py --port 8780

Kept up by the "KL combo-dash" Windows task (register_combo_dash.ps1: at logon
and every 10 min, through run_combo_dash_hidden.vbs -> run_combo_dash.ps1, a
restart loop that logs to run-logs\combo-dash\launcher-<date>.log). Stop it:
create run-logs\combo-dash\combo_dash.stop. A second copy exits 4 (port taken).

What it shows: the maker's status and settings, the combo shard's cash, today's
funnel (quotes -> accepted -> confirmed, and why it declined), the open tickets
(legs, our price vs fair, what each can lose), the settled ones with their P&L,
the fills Kalshi recorded (price, fee), the latest minute's RFQ funnel, and a
feed of the recent events.

READ-ONLY. It reads the maker's files in run-logs\combo-maker (maker_state.json
and combo_<UTC date>.jsonl, or the .jsonl.gz the maker makes of a finished day),
the shadow logger's last stat line (run-logs\combo-rfq), and at most once a
minute three Kalshi reads: the shard balances, today's fills, and the open
tickets' combo markets. It places nothing. It binds 127.0.0.1 only.

A page load reads only the log lines written since the last one (MakerLog), and
the Kalshi reads refresh on a background thread, so a load takes milliseconds
however big the day's log gets (11/06: 133 MB by 9 AM ET had made it 8-22 s).
"""
import argparse
import collections
import contextlib
import gzip
import html
import json
import math
import os
import socket
import sys
import threading
import time
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

MAKER_DIR = os.environ.get("COMBO_MAKER_DIR", os.path.join(HERE, "run-logs", "combo-maker"))
SHADOW_DIR = os.environ.get("COMBO_RFQ_DIR", os.path.join(HERE, "run-logs", "combo-rfq"))
# The "KL combo-dash" task keeps the page up (register_combo_dash.ps1; Jack 2026-11-06
# "Yes, add the task": the app had stopped its preview server when the session went
# idle). Its launcher logs to DASH_DIR; the stop file there ends it (exit 3), and a
# second copy finds the port taken and exits 4, leaving the first one serving.
DASH_DIR = os.environ.get("COMBO_DASH_DIR", os.path.join(HERE, "run-logs", "combo-dash"))
STOP_FILE = os.path.join(DASH_DIR, "combo_dash.stop")
STOP_CHECK_S = 2.0
EXIT_STOP, EXIT_PORT = 3, 4
PORT = 8772
REFRESH_S = 15
KALSHI_TTL_S = 60.0
COMBO_SHARD = 1            # the combo shard (exchange_index): its cash and its fills
RUNNING_S = 120.0          # a maker with no line this recent is shown as stopped
FEED_N = 40
KINDS = {"GAME": "win", "SPREAD": "spread", "TOTAL": "total"}
FEED_KINDS = frozenset(("quote", "would_quote", "accept", "confirm", "decline", "executed", "filled", "unfilled",
                        "settled", "err", "position", "start", "stop"))
COUNT_KINDS = frozenset(("quote", "would_quote", "accept", "confirm", "decline", "executed", "unfilled", "err"))
READ_CHUNK = 8 << 20       # the log is read in 8 MB pieces (a cold start reads a whole day)
BUSY_MAX_S = 120.0         # a background Kalshi read this old is given up on and tried again


def _f(x, dflt=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return dflt


def et(ts) -> datetime:
    try:
        import pytz
        return datetime.fromtimestamp(ts, pytz.timezone("US/Eastern"))
    except ImportError:
        return datetime.fromtimestamp(ts - 5 * 3600, timezone.utc)


def et_midnight(ts: float) -> float:
    d = et(ts)
    return ts - (d.hour * 3600 + d.minute * 60 + d.second + d.microsecond / 1e6)


def leg_label(leg) -> str:
    """KXNBAGAME-26NOV05MIAGSW-GSW + yes -> 'NBA MIA-GSW: GSW win'."""
    tk, side = leg[0], leg[1]
    parts = tk.split("-")
    series = parts[0][2:] if parts[0].startswith("KX") else parts[0]
    kind = ""
    for suf, name in KINDS.items():
        if series.endswith(suf):
            series, kind = series[: -len(suf)], name
            break
    game = parts[1][7:] if len(parts) > 1 else ""
    pick = parts[-1] if len(parts) > 2 else ""
    if kind == "total":                      # YES on a total = over the line
        return f"{series} {game}: {'over' if side == 'yes' else 'under'} {pick}"
    s = f"{series} {game}: {pick} {kind}".strip()
    return s if side == "yes" else f"NOT ({s})"


# ---- the files ---------------------------------------------------------------

def read_events(maker_dir: str, now: float, days: int = 2) -> list:
    """The maker's log lines, oldest first, from the last `days` UTC days.
    Each gets "_ts": its own time ("t", or "at" on quotes and confirms), else
    the last stamped line's before it (accept / decline / err / executed lines
    carry none; the log is written in order)."""
    out, last = [], 0.0
    for d in range(days - 1, -1, -1):
        day = datetime.fromtimestamp(now - d * 86400, timezone.utc).strftime("%Y-%m-%d")
        p = os.path.join(maker_dir, f"combo_{day}.jsonl")
        if os.path.exists(p):
            f = open(p, encoding="utf-8")
        elif os.path.exists(p + ".gz"):
            f = gzip.open(p + ".gz", "rt", encoding="utf-8")
        else:
            continue
        with f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                own = _f(e.get("t")) or _f(e.get("at"))
                if own:
                    last = own
                e["_ts"] = own or last
                out.append(e)
    return out


# The page's own reader (Jack 2026-11-06 "http://127.0.0.1:8772/ broken"): a load
# used to re-parse the whole day's log -- 133 MB and 197,000 quote lines by 9 AM ET,
# 8 s a load and growing all day -- and the maker gzips a day's file at the UTC
# day roll (combo_<day>.jsonl -> .jsonl.gz), which the page then lost, start line
# and all (NOT RUNNING, no config, today's funnel gone). MakerLog keeps a small
# summary per UTC day and parses only the lines appended since the last load;
# quote lines (all but a few hundred) are counted from their "at" without a full
# parse, and only the feed's last few are parsed, when shown.

def _quote_at(line: bytes):
    """A quote line's own time (its "at"), read without parsing the line; None
    when the line is not the maker's plain quote shape (it is parsed in full)."""
    if (b'"k":"quote"' not in line and b'"k": "quote"' not in line) or b'"t":' in line:
        return None
    i = line.find(b'"at":')
    if i < 0:
        return None
    ends = [j for j in (line.find(b",", i + 5), line.find(b"}", i + 5)) if j >= 0]
    if not ends:
        return None
    try:
        return float(line[i + 5:min(ends)]) or None
    except ValueError:
        return None


# ---- segments (Jack 2026-11-06: "add more robust analytics to the dash. segmenting by
# # of legs, sports, etc to understand fill rates, edge, PnL, odds, etc") ----

FAIR_BINS = ((0.05, "under 5¢"), (0.10, "5–10¢"), (0.20, "10–20¢"), (0.30, "20–30¢"), (0.40, "30–40¢"),
             (9.0, "40¢ and up"))
DIMS = (("legs", "Legs"), ("sports", "Sports"), ("odds", "Odds (fair at the sale)"), ("types", "Leg types"),
        ("fee", "Maker fee"))
TYPE_ORDER = ("winners only", "with spreads / totals", "with extras")
FEE_ORDER = ("fee-paying", "all-NFL (no fee)")
SPORT_ROWS = 12                    # the sports table: every mix we hold, then the busiest, the rest folded
RECENT_QUOTES = 50_000             # quote id -> segment, for the accepts and declines that follow
_SERIES = {}                       # series -> (sport, leg kind)


def _series_info(series: str) -> tuple:
    """(sport, kind) of a leg's series: the maker's sport_of (combo_maker) and
    win / spread / total / extra (first half, quarters, team totals, props)."""
    v = _SERIES.get(series)
    if v is None:
        try:
            from combo_rfq_score import league_of, sport_group
            g = sport_group(series)
        except Exception:                       # the scorer's rules unavailable: unsegmented
            g, league_of = "", (lambda s: None)
        if g == "soccer":
            sport = "SOCCER"
        elif g in ("us", "extra", "prop"):
            sport = league_of(series) or "?"
        elif g == "unchecked":
            sport = ("UFC" if series.startswith("KXUFCFIGHT") else "BOXING" if series.startswith("KXBOXING")
                     else "TENNIS")
        else:
            sport = "?"
        kind = ("extra" if g in ("extra", "prop") else "spread" if series.endswith("SPREAD")
                else "total" if series.endswith("TOTAL") else "win")
        v = _SERIES[series] = (sport, kind)
    return v


def fair_bin(fair) -> str:
    if fair is None:
        return "?"
    for top, lab in FAIR_BINS:
        if fair < top:
            return lab
    return FAIR_BINS[-1][1]


def seg_key(legs, fair) -> tuple:
    """A parlay's segment in each of DIMS: (legs, sports, odds, leg types, fee)."""
    infos = [_series_info(str(l[0]).split("-")[0]) for l in legs]
    kinds = {k for _, k in infos}
    return (len(legs), "+".join(sorted({s for s, _ in infos})) or "?", fair_bin(fair),
            "with extras" if "extra" in kinds else "with spreads / totals" if kinds & {"spread", "total"}
            else "winners only",
            "all-NFL (no fee)" if legs and all(str(l[0]).startswith("KXNFL") for l in legs) else "fee-paying")


def _num_after(line: bytes, tag: bytes):
    i = line.find(tag)
    if i < 0:
        return None
    i += len(tag)
    ends = [j for j in (line.find(b",", i), line.find(b"}", i)) if j >= 0]
    try:
        return float(line[i:min(ends)]) if ends else None
    except ValueError:
        return None


def _str_after(line: bytes, tag: bytes):
    i = line.find(tag)
    if i < 0:
        return None
    a = line.find(b'"', i + len(tag))
    b = line.find(b'"', a + 1) if a >= 0 else -1
    return line[a + 1:b].decode("utf-8", "replace") if a >= 0 and b > a else None


def _quote_seg(line: bytes):
    """(segment, markup, quote id) of a quote line, read without a full parse
    (only its legs array is parsed); None when the legs can't be read."""
    i = line.find(b'"legs":')
    j = line.find(b"]]", i) if i >= 0 else -1
    if j < 0:
        return None
    try:
        legs = json.loads(line[i + 7:j + 2])
    except ValueError:
        return None
    return (seg_key(legs, _num_after(line, b'"fair":')), _num_after(line, b'"mu":') or 0.0,
            _str_after(line, b'"quote_id":'))


class _DaySum:
    """One UTC day's maker log, summed as it is read: what the page shows of it."""

    def __init__(self, last: float = 0.0, recent=None, sampler=None):
        self.off = 0                  # bytes of whole lines read
        self.final = False            # read to its end from the .gz: the maker closed the day
        self.last = last              # the last stamped line's time (an unstamped line takes it)
        self.max_ts = 0.0
        self.last_start = None        # the day's last start line, the latest time from it on,
        self.max_after = 0.0          # and whether a stop came after it
        self.stop_after = self.any_stop = False
        self.last_stat = None
        self.counts = {}              # (UTC hour, kind) -> lines (ET midnights fall on whole hours)
        self.whys = {}                # (UTC hour, why) -> declines
        self.quotes = self.accepts = 0
        self.first_quote = None
        self.settle_ts = {}           # combo -> its settled line's time
        self.feed = collections.deque(maxlen=FEED_N)     # parsed lines, or (time, raw) for quotes
        self.seg_q = {}               # segment -> [quotes, markup sum]
        self.seg_acc = {}             # segment -> accepts (by the accepted quote's id)
        self.seg_dec = {}             # segment -> last-look declines
        self.recent = recent if recent is not None else collections.OrderedDict()   # quote id -> segment
        self.sampler = sampler        # QuoteOutcomes: offered the sampled quotes
        self.acc_ids, self.dec_ids = set(), set()   # quote ids accepted by a buyer / declined at the last look

    def _seg_quote(self, key, mu, qid) -> None:
        v = self.seg_q.get(key)
        if v is None:
            v = self.seg_q[key] = [0, 0.0]
        v[0] += 1
        v[1] += mu or 0.0
        if qid:
            r = self.recent
            r[qid] = key
            if len(r) > RECENT_QUOTES:
                r.popitem(last=False)

    def read(self, f, final: bool = False) -> None:
        """The lines from self.off on (f binary). An unfinished last line waits for
        the next read -- the maker may be writing it -- unless final."""
        f.seek(self.off)
        tail = b""
        counts, feed = self.counts, self.feed
        while True:
            chunk = f.read(READ_CHUNK)
            if not chunk:
                break
            data = tail + chunk
            cut = data.rfind(b"\n") + 1
            for line in data[:cut].split(b"\n"):
                if len(line) < 3:
                    continue
                at = _quote_at(line)
                if at is None:
                    self._line(line)
                    continue
                # a quote line (nearly all of them): what _seen does for one, inline
                self.last = at
                if at > self.max_ts:
                    self.max_ts = at
                if self.last_start is not None and at > self.max_after:
                    self.max_after = at
                key = (int(at // 3600) * 3600, "quote")
                counts[key] = counts.get(key, 0) + 1
                self.quotes += 1
                if self.first_quote is None or at < self.first_quote:
                    self.first_quote = at
                feed.append((at, line))
                seg = _quote_seg(line)
                if seg is not None:
                    self._seg_quote(*seg)
                    if self.sampler is not None and _sampled(seg[2]):
                        self.sampler.offer(_quote_rec(seg[2], seg[0], _val_after(line, b'"mt":'),
                                                      _val_after(line, b'"rfq_ms":'), at,
                                                      _val_after(line, b'"ask":'), _val_after(line, b'"fair":'),
                                                      _val_after(line, b'"tc":'), _val_after(line, b'"cn":')))
            self.off += cut
            tail = data[cut:]
        if final and tail.strip():
            self._line(tail)
            self.off += len(tail)

    def _line(self, line: bytes) -> None:
        at = _quote_at(line)
        if at is not None:
            self.last = at
            self._seen("quote", at, None, line)
            return
        try:
            e = json.loads(line)
        except ValueError:
            return
        if not isinstance(e, dict):
            return
        own = _f(e.get("t")) or _f(e.get("at"))
        if own:
            self.last = own
        e["_ts"] = own or self.last
        self._seen(e.get("k"), e["_ts"], e, None)

    def _seen(self, k, ts: float, e, raw) -> None:
        if ts > self.max_ts:
            self.max_ts = ts
        if k == "start":
            self.last_start, self.max_after, self.stop_after = e, ts, False
        elif self.last_start is not None and ts > self.max_after:
            self.max_after = ts
        if k == "stop":
            self.stop_after = self.any_stop = True
        if k in COUNT_KINDS:
            h = int(ts // 3600) * 3600
            self.counts[(h, k)] = self.counts.get((h, k), 0) + 1
            if k == "decline":
                w = e.get("why")
                self.whys[(h, w)] = self.whys.get((h, w), 0) + 1
                key = self.recent.get(e.get("quote_id"))
                if key is not None:
                    self.seg_dec[key] = self.seg_dec.get(key, 0) + 1
                if e.get("quote_id"):
                    self.dec_ids.add(e["quote_id"])
        if k == "quote":
            self.quotes += 1
            if self.first_quote is None or ts < self.first_quote:
                self.first_quote = ts
            if e is not None and e.get("legs"):
                key = seg_key(e["legs"], _f(e.get("fair")))
                self._seg_quote(key, _f(e.get("mu"), 0.0), e.get("quote_id"))
                if self.sampler is not None and _sampled(e.get("quote_id")):
                    self.sampler.offer(_quote_rec(e["quote_id"], key, e.get("mt"), e.get("rfq_ms"), ts,
                                                  e.get("ask"), e.get("fair"), e.get("tc"), e.get("cn")))
        elif k == "accept":
            self.accepts += 1
            key = self.recent.get(e.get("quote_id"))
            if key is not None:
                self.seg_acc[key] = self.seg_acc.get(key, 0) + 1
            if e.get("quote_id"):
                self.acc_ids.add(e["quote_id"])
        elif k == "stat":
            self.last_stat = e
        elif k == "settled":
            self.settle_ts[e.get("mt")] = ts
        if k in FEED_KINDS:
            self.feed.append(e if e is not None else (ts, raw))


def _feed_event(x) -> dict:
    if not isinstance(x, tuple):
        return x
    ts, raw = x
    try:
        e = json.loads(raw)
    except ValueError:
        e = {"k": "quote"}
    e["_ts"] = ts
    return e


class MakerLog:
    """The maker's last `days` UTC days of log, one _DaySum each, read as they
    grow; a day the maker has gzipped is read to its end once from the .gz."""

    def __init__(self, maker_dir: str, days: int = 2, sampler=None):
        self.dir, self.days = maker_dir, days
        self.sums = {}                # UTC day -> _DaySum
        self.recent = collections.OrderedDict()     # quote id -> segment, across the days
        self.sampler = sampler                      # QuoteOutcomes (the server's), or None
        self.lock = threading.Lock()

    def update(self, now: float) -> list:
        """The days' summaries, oldest first, read up to now."""
        with self.lock:
            want = [datetime.fromtimestamp(now - d * 86400, timezone.utc).strftime("%Y-%m-%d")
                    for d in range(self.days - 1, -1, -1)]
            for day in list(self.sums):
                if day not in want:
                    del self.sums[day]
            out, last = [], 0.0
            for day in want:
                s = self._read(day, self.sums.get(day), last)
                if s is None:
                    self.sums.pop(day, None)
                    continue
                self.sums[day] = s
                out.append(s)
                last = s.last
            return out

    def _read(self, day: str, s, last: float):
        p = os.path.join(self.dir, f"combo_{day}.jsonl")
        try:
            if os.path.exists(p):
                if s is None or s.final or os.path.getsize(p) < s.off:
                    s = _DaySum(last, self.recent, self.sampler)    # new, or the file was replaced
                with open(p, "rb") as f:
                    s.read(f)
                return s
            if os.path.exists(p + ".gz"):
                if s is not None and s.final:
                    return s
                s = s or _DaySum(last, self.recent, self.sampler)   # the rest of a day read while plain
                with gzip.open(p + ".gz", "rb") as f:
                    s.read(f, final=True)
                s.final = True
                return s
        except (OSError, EOFError, zlib.error):             # a file mid-roll: keep what was read
            return s
        return None


def read_state(maker_dir: str) -> list:
    p = os.path.join(maker_dir, "maker_state.json")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("tickets") or []
    except (OSError, ValueError):
        return []


def shadow_pulse(shadow_dir: str, now: float):
    """The shadow logger's last minute stat (the whole market's combo flow),
    read from the tail of today's file."""
    day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    p = os.path.join(shadow_dir, f"combo_{day}.jsonl")
    if not os.path.exists(p):
        return None
    try:
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        if '"k":"stat"' in line:
            try:
                return json.loads(line)
            except ValueError:
                return None
    return None


# ---- Kalshi (cached, read-only) -----------------------------------------------

class KalshiReads:
    """Shard cash, today's combo fills and the open combos' markets, refreshed
    at most every KALSHI_TTL_S; a failed read keeps the last good one. Only the
    first read is made inline; after it a refresh runs on a background thread and
    a page gets the last reads meanwhile (11/06: a load waited ~2 s on these once
    a minute, and ~3 s on the candles every 5 min)."""

    def __init__(self, client_factory=None, ttl: float = KALSHI_TTL_S, background: bool = True):
        self.factory = client_factory
        self.ttl = ttl
        self.background = background
        self.client = None
        self.at = 0.0
        self.busy, self.busy_since, self.tries, self.thread = False, 0.0, 0, None
        self.data = {"cash": None, "fills": [], "markets": {}, "error": None, "at": None}
        self.lock = threading.Lock()
        self.io = threading.Lock()            # one read at a time on the shared client (the candles' too)

    def _client(self):
        if self.client is None:
            if self.factory is not None:
                self.client = self.factory()
            else:
                os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")
                import kalshi_subaccount as ks
                self.client = ks.client()
        return self.client

    def get(self, now: float, open_tickers, day_start: float, since=None) -> dict:
        with self.lock:
            if now - self.at < self.ttl or (self.busy and now - self.busy_since < BUSY_MAX_S):
                return self.data
            self.at, self.busy, self.busy_since = now, True, now
            inline = not self.background or self.tries == 0
            self.tries += 1
        args = (now, sorted(set(open_tickers)), day_start, since)
        if inline:
            self._refresh(*args)
        else:
            self.thread = threading.Thread(target=self._refresh, args=args, daemon=True)
            self.thread.start()
        return self.data

    def _refresh(self, now: float, tks: list, day_start: float, since=None) -> None:
        try:
            with self.io:
                c = self._client()
                bal = c.get("/portfolio/balance", {})
                cash = {int(r.get("exchange_index")): _f(r.get("balance"), 0.0)
                        for r in bal.get("balance_breakdown") or []}
                # The combo shard's fills only (Jack 2026-11-06: make the two fee figures agree).
                # The account's feed is mostly the IMM's, and its one page of 200 had pushed the
                # older combo fills off (19 of 29 shown, $5.77 of $7.98 in fees). From the first
                # ticket's fill (`since`), so this card and the P&L count the same fills.
                fills, cursor = [], None
                min_ts = int(since) if since else int(day_start - 86400)
                for _ in range(10):
                    params = {"min_ts": min_ts, "limit": 200, "exchange_index": COMBO_SHARD}
                    if cursor:
                        params["cursor"] = cursor
                    fj = c.get("/portfolio/fills", params)
                    fills += [x for x in fj.get("fills") or []
                              if str(x.get("market_ticker") or x.get("ticker") or "").startswith("KXMVE")]
                    cursor = fj.get("cursor")
                    if not cursor:
                        break
                mk = {}
                for i in range(0, len(tks), 100):
                    mj = c.get("/markets", {"tickers": ",".join(tks[i:i + 100]), "limit": 1000})
                    for m in mj.get("markets") or []:
                        mk[m.get("ticker")] = m
            data = {"cash": cash.get(COMBO_SHARD), "fills": fills, "markets": mk, "error": None, "at": now}
        except Exception as e:
            data = dict(self.data, error=f"{type(e).__name__}: {str(e)[:160]}")
        with self.lock:
            self.data, self.busy = data, False


# ---- P&L (Jack 2026-11-06: "show total pnl, settled and unrealized, premiums and trading") ----

def leg_value(leg, markets) -> float:
    """A leg's value for its side: its book's mid, or 1 / 0 once it has a result
    (None: no two-sided book and no result)."""
    tk, side = leg[0], leg[1]
    m = markets.get(tk) or {}
    r = str(m.get("result") or "").lower()
    if r in ("yes", "no"):
        p = 1.0 if r == "yes" else 0.0
    else:
        bid, ask = _f(m.get("yes_bid_dollars"), 0.0), _f(m.get("yes_ask_dollars"), 0.0)
        if not (0.0 < bid < ask <= 1.0):
            return None
        p = (bid + ask) / 2.0
    return p if side == "yes" else 1.0 - p


def ticket_value(t: dict, markets) -> float:
    """The parlay's fair now: the product of its legs' values (None if one has no price)."""
    v = 1.0
    for leg in t["legs"]:
        x = leg_value(leg, markets)
        if x is None:
            return None
        v *= x
    return v


def pnl_parts(open_t, settled, markets) -> dict:
    """Total = edge at sale + trading - fees, split settled / unrealized.
    Edge at sale ("premium" in the data): what we sold over fair at the quote,
    contracts x (our price - the legs' mid product then). Trading: how the parlay moved since, contracts x
    (fair then - fair now), or x (fair then - payout) once settled. An open
    ticket with a leg unpriced stays at its quote's fair (no trading yet)."""
    out = {"premium": 0.0, "trading": 0.0, "fees": 0.0, "settled": 0.0, "unrealized": 0.0,
           "collected": 0.0, "unpriced": 0}
    for t in settled:
        c, ask, fee = t["contracts"], t["ask"], _f(t.get("fee_est"), 0.0) or 0.0
        fair = _f(t.get("fair"), ask)
        pay = 1.0 if t.get("result") == "yes" else 0.0
        out["premium"] += c * (ask - fair)
        out["trading"] += c * (fair - pay)
        out["fees"] += fee
        out["settled"] += c * (ask - pay) - fee
        out["collected"] += c * ask
    for t in open_t:
        c, ask, fee = t["contracts"], t["ask"], _f(t.get("fee_est"), 0.0) or 0.0
        fair = _f(t.get("fair"), ask)
        v = ticket_value(t, markets)
        if v is None:
            out["unpriced"] += 1
            v = fair
        t["_value"] = v
        t["_pnl"] = c * (ask - v) - fee
        out["premium"] += c * (ask - fair)
        out["trading"] += c * (fair - v)
        out["fees"] += fee
        out["unrealized"] += t["_pnl"]
        out["collected"] += c * ask
    out["total"] = out["settled"] + out["unrealized"]
    return out


def _order(dim: str, rows: list) -> list:
    if dim == "legs":
        return sorted(rows, key=lambda r: r["seg"] if isinstance(r["seg"], int) else 99)
    if dim in ("odds", "types", "fee"):
        order = {"odds": [lab for _, lab in FAIR_BINS] + ["?"], "types": list(TYPE_ORDER),
                 "fee": list(FEE_ORDER)}[dim]
        return sorted(rows, key=lambda r: order.index(r["seg"]) if r["seg"] in order else 99)
    # sports: every mix we hold a ticket on, then the busiest by quotes; the rest folded together
    keep = [r for r in rows if r["tickets"]]
    rest = sorted((r for r in rows if not r["tickets"]), key=lambda r: -r["quotes"])
    room = max(0, SPORT_ROWS - len(keep))
    shown = sorted(keep + rest[:room], key=lambda r: (-r["quotes"], -r["tickets"]))
    folded = rest[room:]
    if folded:
        other = {"seg": f"{len(folded)} other mixes"}
        for k in folded[0]:
            if k != "seg":
                other[k] = sum(r[k] for r in folded)
        shown.append(other)
    return shown


def analytics(days, tickets, since, outcomes=None) -> dict:
    """Per dimension in DIMS, one row a segment: the quoting the page's log holds
    (quotes, the markup asked, accepts, last-look declines, fills from `since`, the
    first quote there) and every filled ticket (collected, edge at sale, trading,
    fees, P&L, settled hits against the fair's expectation). Run after pnl_parts,
    which values the open tickets, so the P&L rows add up to the cards."""
    q, acc, dec = {}, collections.Counter(), collections.Counter()
    for d in days:
        for key, (n, mus) in d.seg_q.items():
            v = q.setdefault(key, [0, 0.0])
            v[0] += n
            v[1] += mus
        acc.update(d.seg_acc)
        dec.update(d.seg_dec)
    tix = []
    for t in filled_tickets(tickets):
        c, ask = t["contracts"], t["ask"]
        fair = _f(t.get("fair"), ask)
        settled = t.get("status") == "settled"
        v = (1.0 if t.get("result") == "yes" else 0.0) if settled else _f(t.get("_value"), fair)
        tix.append((seg_key(t["legs"], fair), t, c, ask, fair, v, settled))
    dims = {}
    for di, (dim, _label) in enumerate(DIMS):
        rows = {}

        def row(name, rows=rows):
            return rows.setdefault(name, {"seg": name, "quotes": 0, "mu_sum": 0.0, "accepted": 0, "declined": 0,
                                          "filled": 0, "tickets": 0, "contracts": 0.0, "collected": 0.0,
                                          "edge": 0.0, "trading": 0.0, "fees": 0.0, "pnl": 0.0, "open": 0,
                                          "can_lose": 0.0, "settled": 0, "hits": 0, "exp_hits": 0.0,
                                          "checked": 0, "o_not": 0, "o_cheaper": 0, "o_same": 0, "o_dearer": 0,
                                          "o_other": 0, "gap_cheaper": 0.0})
        for key, (n, mus) in q.items():
            r = row(key[di])
            r["quotes"] += n
            r["mu_sum"] += mus
        for key, n in acc.items():
            row(key[di])["accepted"] += n
        for key, n in dec.items():
            row(key[di])["declined"] += n
        for key, t, c, ask, fair, v, settled in tix:
            r = row(key[di])
            fee = _f(t.get("fee_est"), 0.0) or 0.0
            if since is not None and fill_time(t) >= since:
                r["filled"] += 1
            r["tickets"] += 1
            r["contracts"] += c
            r["collected"] += c * ask
            r["edge"] += c * (ask - fair)
            r["trading"] += c * (fair - v)
            r["fees"] += fee
            r["pnl"] += c * (ask - v) - fee
            if settled:
                r["settled"] += 1
                r["hits"] += 1 if t.get("result") == "yes" else 0
                r["exp_hits"] += fair
            else:
                r["open"] += 1
                r["can_lose"] += c * (1.0 - ask)
        for o in outcomes or []:
            key = o.get("key")
            if not key or len(key) != len(DIMS) or o.get("outcome") not in OUTCOME_KEY:
                continue
            r = row(key[di])
            r["checked"] += 1
            r[OUTCOME_KEY[o["outcome"]]] += 1
            if o["outcome"] == "cheaper elsewhere":
                r["gap_cheaper"] += _f(o.get("gap"), 0.0) or 0.0
        dims[dim] = _order(dim, list(rows.values()))
    return {"since": since, "dims": dims}


# ---- the snapshot ----------------------------------------------------------------

def snapshot(maker_dir: str, shadow_dir: str, now: float, kalshi=None, history=None, logs=None,
             outcomes=None) -> dict:
    """The page's numbers. `logs`: the server's MakerLog (only new lines are read);
    without one the log is read whole."""
    days = (logs if logs is not None else MakerLog(maker_dir)).update(now)
    tickets = read_state(maker_dir)
    oc_rows = None
    if outcomes is not None:                         # the accepted quotes are the maker's own records
        outcomes.note_accepted(set().union(*[d.acc_ids for d in days]) if days else set())
        fq = [d.first_quote for d in days if d.first_quote is not None]
        oc_rows = outcomes.results(min(fq) if fq else None)
    day0 = et_midnight(now)
    last_start, stopped, last_t = None, False, 0.0
    for d in days:                                   # oldest first: the last start, and what came after
        if d.last_start is not None:
            last_start, stopped, last_t = d.last_start, d.stop_after, d.max_after
        elif last_start is not None:
            stopped, last_t = stopped or d.any_stop, max(last_t, d.max_ts)
    running = bool(last_start) and not stopped and now - last_t <= RUNNING_S
    today = day0 - 1.0                               # (hour buckets; day0 is a whole hour up to float error)
    count = lambda k: sum(n for d in days for (h, kk), n in d.counts.items() if kk == k and h >= today)  # noqa: E731
    declines = {}
    for d in days:
        for (h, w), n in d.whys.items():
            if h >= today:
                declines[w] = declines.get(w, 0) + n
    stats = [d.last_stat for d in days if d.last_stat is not None]
    settle_ts = {}
    for d in days:
        settle_ts.update(d.settle_ts)
    firsts = [d.first_quote for d in days if d.first_quote is not None]
    open_t = [t for t in tickets if t.get("status") in ("confirmed", "executed")]
    settled = [t for t in tickets if t.get("status") == "settled"]
    filled = filled_tickets(tickets)
    since = (min(fill_time(t) for t in filled) - 60.0) if filled else None   # the fills the P&L counts
    k = None
    if kalshi is not None:
        k = kalshi.get(now, [t["mt"] for t in open_t] + [l[0] for t in open_t for l in t["legs"]], day0, since)
    fills = (k or {}).get("fills") or []
    pnl = pnl_parts(open_t, settled, (k or {}).get("markets") or {})
    hist = history.get(now, filled, (k or {}).get("markets") or {}, settle_ts) if history is not None else None
    series = [p for p in (hist or {}).get("series") or [] if p["t"] < now]
    if filled:                                     # the last point is the cards' live P&L
        series.append({"t": now, "premium": pnl["premium"], "trading": pnl["trading"], "fees": pnl["fees"],
                       "total": pnl["total"]})
    return {
        "now": now, "running": running, "stopped": stopped,
        "live": bool(last_start and last_start.get("live")),
        "cfg": (last_start or {}).get("cfg") or {},
        "started": _f((last_start or {}).get("t")),
        "last_line_age": (now - last_t) if last_t else None,
        "funnel": {"quotes": count("quote"), "would": count("would_quote"), "accepted": count("accept"),
                   "confirmed": count("confirm"), "declined": count("decline"),
                   "executed": count("executed") - count("unfilled"), "unfilled": count("unfilled"),
                   "errors": count("err")},
        "declines": declines,
        "open": open_t, "settled": settled,
        "open_loss": sum(t["contracts"] * (1.0 - t["ask"]) for t in open_t),
        "open_premium": sum(t["contracts"] * t["ask"] for t in open_t),
        "settled_pnl": sum(_f(t.get("pnl"), 0.0) for t in settled),
        "pnl": pnl,
        "series": series, "fill_points": fill_points(filled),
        "analytics": analytics(days, tickets, min(firsts) if firsts else None, oc_rows),
        "outcomes": dict(outcomes.state(), sample_pct=OUTCOME_SAMPLE_PCT) if outcomes is not None else None,
        "fill_rate": _fill_rate(sum(d.quotes for d in days), sum(d.accepts for d in days),
                                min(firsts) if firsts else None, filled),
        "history_error": (hist or {}).get("error"),
        "wins": sum(1 for t in settled if t.get("result") == "no"),
        "losses": sum(1 for t in settled if t.get("result") == "yes"),
        "last_stat": stats[-1] if stats else None,
        "feed": [_feed_event(x) for x in [x for d in days for x in d.feed][-FEED_N:]][::-1],
        "kalshi": k, "fees": sum(_f(x.get("fee_cost"), 0.0) for x in fills), "fills": fills,
        "pulse": shadow_pulse(shadow_dir, now),
    }


def _ev_ts(e) -> float:
    v = _f(e.get("_ts"))
    if v:
        return v
    for key in ("t", "at"):
        v = _f(e.get(key))
        if v:
            return v
    return 0.0


# ---- the page ----------------------------------------------------------------------

def usd(x, d=2) -> str:
    if x is None:
        return "&ndash;"
    return ("&minus;" if x < 0 else "") + f"${abs(x):,.{d}f}"


def signed(x) -> str:
    if x is None:
        return "&ndash;"
    cls = "pos" if x > 0 else "neg" if x < 0 else ""
    return f'<span class="{cls}">{"+" if x > 0 else "&minus;" if x < 0 else ""}${abs(x):,.2f}</span>'


def tm(ts) -> str:
    return et(ts).strftime("%-I:%M:%S %p") if os.name != "nt" else et(ts).strftime("%I:%M:%S %p").lstrip("0")


def legs_html(legs) -> str:
    return "<br>".join(html.escape(leg_label(l)) for l in legs)


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--muted:#6b7280;--line:#e5e7eb;--pos:#15803d;--neg:#b91c1c;
--accent:#2563eb;--warn:#b45309;--chip:#eef2ff}
@media (prefers-color-scheme:dark){:root{--bg:#0f1218;--card:#171b23;--ink:#e6e9ef;--muted:#9aa3b2;--line:#2a303b;
--pos:#4ade80;--neg:#f87171;--accent:#60a5fa;--warn:#fbbf24;--chip:#1e2533}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,Segoe UI,Arial,sans-serif}
main{max-width:1180px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:22px 0 8px}
.top{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:space-between}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:700;font-size:12px;letter-spacing:.04em}
.live{background:#dcfce7;color:#166534}.dry{background:#e0e7ff;color:#3730a3}.off{background:#fee2e2;color:#991b1b}
@media (prefers-color-scheme:dark){.live{background:#14532d;color:#bbf7d0}.dry{background:#1e1b4b;color:#c7d2fe}.off{background:#450a0a;color:#fecaca}}
.muted{color:var(--muted)}.small{font-size:12px}.pos{color:var(--pos)}.neg{color:var(--neg)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-top:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card .v{font-size:22px;font-weight:700;margin-top:2px}.card .l{font-size:12px;color:var(--muted)}
.funnel{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:4px}
.funnel b{font-size:18px}.arrow{color:var(--muted)}
.tw{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-size:11px;color:var(--muted);font-weight:600;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right;white-space:nowrap}
.k{display:inline-block;min-width:74px;font-size:11px;font-weight:700;padding:1px 6px;border-radius:6px;background:var(--chip);text-align:center}
.k.confirm,.k.executed,.k.filled{color:var(--pos)}.k.decline,.k.err,.k.unfilled{color:var(--neg)}.k.accept{color:var(--accent)}
.empty{padding:14px;color:var(--muted)}
.chips span{display:inline-block;background:var(--chip);border-radius:6px;padding:1px 7px;margin:2px 4px 2px 0;font-size:12px}
footer{margin:20px 0 6px;color:var(--muted);font-size:12px}
tr.tot td{font-weight:700;border-top:2px solid var(--line);border-bottom:0}
"""


# ---- history: the P&L and fills over time (Jack 2026-11-06: "add time series of PnL and time series of fills") ----

HISTORY_TTL_S = 300.0               # the legs' candles: re-read at most every 5 min...
HISTORY_MIN_GAP_S = 60.0            # ...or on a new fill / settlement, but not more than once a minute
HISTORY_STEP_S = 300.0              # one P&L point every 5 min (from 1-min candles)


def fill_time(t: dict) -> float:
    return _f(t.get("executed_at")) or _f(t.get("at")) or 0.0


def filled_tickets(tickets) -> list:
    return [t for t in tickets if t.get("status") in ("confirmed", "executed", "settled") and _f(t.get("contracts"))]


def _mid_at(points, t: float):
    """The YES mid of the last candle that closed at or before t (None before the first)."""
    lo, hi = 0, len(points)
    while lo < hi:
        mid = (lo + hi) // 2
        if points[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return points[lo - 1][1] if lo else None


def pnl_series(filled, leg_hist: dict, markets: dict, settle_ts: dict, now: float,
               step: float = HISTORY_STEP_S) -> list:
    """Edge at sale, trading, fees and total at every step from the first fill: each
    ticket from its fill on, its parlay valued at its legs' candle mids then (1 / 0
    once a leg has a result and no later candle; a settled ticket at its payout from
    its settlement on); before a leg's first candle the ticket sits at its quote fair."""
    if not filled:
        return []
    t0 = min(fill_time(t) for t in filled)
    grid, g = [], (t0 // step + 1) * step
    while g < now:
        grid.append(g)
        g += step
    out = []
    for g in grid:
        prem = trad = fees = 0.0
        for t in filled:
            if fill_time(t) > g:
                continue
            c, ask, fee = t["contracts"], t["ask"], _f(t.get("fee_est"), 0.0) or 0.0
            fair = _f(t.get("fair"), ask)
            if t.get("status") == "settled" and g >= (settle_ts.get(t.get("mt")) or now):
                v = 1.0 if t.get("result") == "yes" else 0.0
            else:
                v = 1.0
                for tk, side in t["legs"]:
                    pts = leg_hist.get(tk) or []
                    res = str((markets.get(tk) or {}).get("result") or "").lower()
                    if res in ("yes", "no") and (not pts or g >= pts[-1][0]):
                        p = 1.0 if res == "yes" else 0.0
                    else:
                        p = _mid_at(pts, g)
                    if p is None:
                        v = None
                        break
                    v *= p if side == "yes" else 1.0 - p
                if v is None:
                    v = fair
            prem += c * (ask - fair)
            trad += c * (fair - v)
            fees += fee
        out.append({"t": g, "premium": prem, "trading": trad, "fees": fees, "total": prem + trad - fees})
    return out


def fill_rate(events, filled) -> dict:
    """Quotes filled (Jack 2026-11-06: "show a stat for % quotes filled"): Kalshi
    fills / quotes sent, over the maker's logs the page reads (since its first
    quote there), with the accepted share beside it."""
    qs = [_ev_ts(e) for e in events if e.get("k") == "quote"]
    return _fill_rate(len(qs), sum(1 for e in events if e.get("k") == "accept"), min(qs) if qs else None, filled)


def _fill_rate(quotes: int, accepted: int, since, filled) -> dict:
    if not quotes:
        return {}
    return {"quotes": quotes, "accepted": accepted,
            "filled": sum(1 for t in filled if fill_time(t) >= since), "since": since,
            "taker": sum(1 for t in filled if fill_time(t) >= since and t.get("taker_fill"))}


def fill_points(filled) -> list:
    out = []
    for t in sorted(filled, key=fill_time):
        c, ask = t["contracts"], t["ask"]
        out.append({"t": fill_time(t), "premium": c * ask, "lose": c * (1.0 - ask), "ask": ask, "contracts": c,
                    "status": t.get("status"), "result": t.get("result"),
                    "legs": " · ".join(leg_label(l) for l in t["legs"])})
    return out


class HistoryReads:
    """The filled tickets' legs' 1-minute candles since the first fill (hourly
    when that is over 12 h ago; Kalshi's candles come in 1, 60 or 1440 minutes --
    a 5 returned one candle a market), read through the Kalshi reads' client and kept for
    HISTORY_TTL_S, or re-read sooner (>= HISTORY_MIN_GAP_S) on a new fill or
    settlement; the series is computed once per read. A failed read keeps the
    last good one. Like the Kalshi reads, only the first is made inline; later
    ones run on a background thread while pages get the last series."""

    def __init__(self, kalshi, ttl: float = HISTORY_TTL_S, background: bool = True):
        self.kalshi, self.ttl, self.background = kalshi, ttl, background
        self.at, self.key = 0.0, None
        self.busy, self.busy_since, self.tries, self.thread = False, 0.0, 0, None
        self.data = {"legs": {}, "series": [], "error": None, "at": None}
        self.lock = threading.Lock()

    def get(self, now: float, filled, markets: dict, settle_ts: dict) -> dict:
        if not filled:
            return {"legs": {}, "series": [], "error": None, "at": None}
        key = tuple(sorted((str(t.get("quote_id")), str(t.get("status"))) for t in filled))
        with self.lock:
            stale = now - self.at >= self.ttl or (key != self.key and now - self.at >= HISTORY_MIN_GAP_S)
            if not stale or (self.busy and now - self.busy_since < BUSY_MAX_S):
                return self.data
            self.at, self.key, self.busy, self.busy_since = now, key, True, now
            inline = not self.background or self.tries == 0
            self.tries += 1
        args = (now, list(filled), dict(markets), dict(settle_ts))
        if inline:
            self._refresh(*args)
        else:
            self.thread = threading.Thread(target=self._refresh, args=args, daemon=True)
            self.thread.start()
        return self.data

    def _refresh(self, now: float, filled: list, markets: dict, settle_ts: dict) -> None:
        try:
            with getattr(self.kalshi, "io", None) or contextlib.nullcontext():
                c = self.kalshi._client()
                t0 = min(fill_time(t) for t in filled)
                period = 1 if now - t0 <= 12 * 3600 else 60
                per = 3 if period == 1 else 10              # <= ~2,200 candles a call (20 x 480 was refused)
                start = max(t0 - 600, now - 4 * 86400)
                legs = sorted({l[0] for t in filled for l in t["legs"]})
                hist = {}
                for i in range(0, len(legs), per):
                    js = c.get("/markets/candlesticks", {"market_tickers": ",".join(legs[i:i + per]),
                                                          "start_ts": int(start), "end_ts": int(now),
                                                          "period_interval": period})
                    for x in js.get("markets") or []:
                        pts = []
                        for k in x.get("candlesticks") or []:
                            b = _f((k.get("yes_bid") or {}).get("close_dollars"), 0.0) or 0.0
                            a = _f((k.get("yes_ask") or {}).get("close_dollars"), 0.0) or 0.0
                            if 0.0 < b < a <= 1.0 and k.get("end_period_ts"):
                                pts.append((float(k["end_period_ts"]), (a + b) / 2.0))
                        hist[x.get("market_ticker")] = sorted(pts)
            data = {"legs": hist, "series": pnl_series(filled, hist, markets, settle_ts, now),
                    "error": None, "at": now}
        except Exception as e:
            data = dict(self.data, error=f"{type(e).__name__}: {str(e)[:160]}")
        with self.lock:
            self.data, self.busy = data, False


# ---- what became of our quotes (Jack 2026-11-06: "also track quotes based on what
# ultimately fills, vs our price being too high, etc") ----

OUTCOME_SAMPLE_PCT = 2          # the quotes checked: crc32(quote id) % 100 under this -- unbiased, the same after a restart
OUTCOME_CALLS_PER_MIN = 12      # trade-tape reads a minute, one a quote (2% of the maker's 600 quotes a minute)
OUTCOME_WINDOW_S = 120.0        # an RFQ executes within this of its creation (an RFQ lives ~39 s at the p90)
OUTCOME_QUEUE = 240             # quotes waiting for their window to close; the oldest are dropped (a restart
                                # offers the day's whole sample, of which this keeps the newest)
OUTCOME_KINDS = (("not bought", "o_not"), ("cheaper elsewhere", "o_cheaper"), ("same price elsewhere", "o_same"),
                 ("dearer elsewhere", "o_dearer"), ("other print", "o_other"))
OUTCOME_KEY = dict(OUTCOME_KINDS)


def _sampled(qid) -> bool:
    return bool(qid) and zlib.crc32(str(qid).encode("utf-8", "replace")) % 100 < OUTCOME_SAMPLE_PCT


def _val_after(line: bytes, tag: bytes):
    """The JSON scalar after `tag` ('"key":'): a string, a number, or None (null or missing)."""
    i = line.find(tag)
    if i < 0:
        return None
    i += len(tag)
    while line[i:i + 1] in (b" ", b"\t"):
        i += 1
    if line[i:i + 1] == b'"':
        j = line.find(b'"', i + 1)
        return line[i + 1:j].decode("utf-8", "replace") if j > i else None
    if line[i:i + 4] == b"null":
        return None
    ends = [j for j in (line.find(b",", i), line.find(b"}", i)) if j >= 0]
    try:
        return float(line[i:min(ends)]) if ends else None
    except ValueError:
        return None


def _quote_rec(qid, key, mt, rfq_ms, at, ask, fair, tc, cn) -> dict:
    return {"qid": qid, "key": list(key), "mt": mt, "rfq_s": (_f(rfq_ms) or at * 1000.0) / 1000.0, "at": at,
            "ask": _f(ask), "fair": _f(fair), "tc": _f(tc), "cn": _f(cn)}


def _same_size(q: dict, t: dict) -> bool:
    """Is this print the RFQ's own execution? Its size matches the request: a
    target cost ~ contracts x (price + the 0.07 p(1-p) taker fee) -- or the price
    alone, on a fee-free all-NFL combo -- or the contracts asked for."""
    ct = _f(t.get("count_fp"), 0.0) or 0.0
    side = t.get("taker_side") or t.get("taker_outcome_side")
    py = _f(t.get("yes_price_dollars"), 0.0) or 0.0
    p = py if side == "yes" else (_f(t.get("no_price_dollars")) or 1.0 - py)
    if q.get("cn"):
        return abs(ct - q["cn"]) <= max(0.02, 0.01 * ct)
    if q.get("tc"):
        tol = max(0.05, 0.02 * q["tc"])
        return abs(ct * (p + 0.07 * p * (1.0 - p)) - q["tc"]) <= tol or abs(ct * p - q["tc"]) <= tol
    return True


def classify(q: dict, trades: list) -> dict:
    """What became of one of our quotes, from its combo's trade tape between the
    RFQ's creation and OUTCOME_WINDOW_S after: not bought; bought elsewhere
    cheaper than our price (ours too high), at it, or dearer (we were cheaper
    but not taken); or a print that isn't this RFQ's (another size, NO bought)."""
    if not trades:
        return {"outcome": "not bought"}
    for t in sorted(trades, key=lambda t: str(t.get("created_time") or "")):
        if not _same_size(q, t):
            continue
        if (t.get("taker_side") or t.get("taker_outcome_side")) != "yes":
            return {"outcome": "other print", "why": "NO bought"}
        px, ask = _f(t.get("yes_price_dollars")), q.get("ask")
        if px is None or not ask:
            return {"outcome": "other print", "why": "no price"}
        o = ("cheaper elsewhere" if px < ask - 1e-6 else "dearer elsewhere" if px > ask + 1e-6
             else "same price elsewhere")
        return {"outcome": o, "px": px, "gap": round(px / ask - 1.0, 5)}
    return {"outcome": "other print", "why": "another size"}


class QuoteOutcomes:
    """What became of a sample of our quotes. The log reader offers every sampled
    quote (_sampled); once its RFQ's window has closed, a background thread reads
    its combo's trade tape (GET /markets/trades, OUTCOME_CALLS_PER_MIN a minute,
    through the Kalshi reads' client) and classifies it. Quotes the buyer accepted
    -- we filled, declined, or Kalshi canceled -- are the maker's own records,
    counted exactly elsewhere, and are not read. Results append to
    DASH_DIR/quote_outcomes_<UTC day>.jsonl and are read back at a restart."""

    def __init__(self, kalshi, out_dir: str = None, clock=time.time):
        self.kalshi, self.dir, self.clock = kalshi, out_dir or DASH_DIR, clock
        self.queue = collections.deque(maxlen=OUTCOME_QUEUE)
        self.done = {}                  # quote id -> result
        self.skip = set()               # quote ids a buyer accepted
        self.lock = threading.Lock()
        self.calls, self.error, self.thread = 0, None, None
        self.stopping = threading.Event()
        self._load()

    def _path(self, ts: float) -> str:
        return os.path.join(self.dir, f"quote_outcomes_{datetime.fromtimestamp(ts, timezone.utc):%Y-%m-%d}.jsonl")

    def _load(self) -> None:
        now = self.clock()
        for d in (1, 0):
            p = self._path(now - d * 86400)
            if not os.path.exists(p):
                continue
            try:
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        if r.get("qid"):
                            self.done[r["qid"]] = r
            except OSError:
                pass

    def offer(self, q: dict) -> None:
        with self.lock:
            if q.get("qid") and q.get("mt") and q["qid"] not in self.done:
                self.queue.append(q)

    def note_accepted(self, qids) -> None:
        with self.lock:
            self.skip.update(qids)

    def due(self, now: float):
        """The oldest queued quote whose window has closed (None: none yet)."""
        with self.lock:
            while self.queue:
                q = self.queue[0]
                if q["qid"] in self.done or q["qid"] in self.skip:
                    self.queue.popleft()
                    continue
                if (q.get("rfq_s") or q.get("at") or 0.0) + OUTCOME_WINDOW_S + 10.0 > now:
                    return None
                return self.queue.popleft()
            return None

    def check(self, q: dict) -> dict:
        t0 = q.get("rfq_s") or q.get("at")
        with getattr(self.kalshi, "io", None) or contextlib.nullcontext():
            js = self.kalshi._client().get("/markets/trades", {"ticker": q["mt"], "min_ts": int(t0 - 2),
                                                               "max_ts": int(t0 + OUTCOME_WINDOW_S), "limit": 100})
        self.calls += 1
        trades = js.get("trades") or []
        res = dict(classify(q, trades), qid=q["qid"], key=q.get("key"), t=t0, at=q.get("at") or t0, ask=q.get("ask"),
                   fair=q.get("fair"), mt=q.get("mt"), prints=len(trades))
        with self.lock:
            self.done[q["qid"]] = res
        try:
            os.makedirs(self.dir, exist_ok=True)
            with open(self._path(t0), "a", encoding="utf-8") as f:
                f.write(json.dumps(res, separators=(",", ":")) + "\n")
        except OSError as e:
            self.error = f"write: {e}"
        return res

    def step(self, now=None) -> bool:
        """Read one due quote's tape (True if there was one)."""
        q = self.due(self.clock() if now is None else now)
        if q is None:
            return False
        try:
            self.check(q)
            self.error = None
        except Exception as e:
            self.error = f"{type(e).__name__}: {str(e)[:160]}"
        return True

    def start(self) -> None:
        def loop():
            while not self.stopping.wait(60.0 / OUTCOME_CALLS_PER_MIN):
                self.step()
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()

    def results(self, since=None) -> list:
        with self.lock:
            return [r for r in self.done.values()                 # by the quote's time, as the page's window
                    if since is None or (r.get("at") or r.get("t") or 0.0) >= since]

    def state(self) -> dict:
        with self.lock:
            return {"queue": len(self.queue), "calls": self.calls, "error": self.error, "checked": len(self.done)}


# ---- the charts (inline SVG, no libraries; the dataviz reference palette's first three slots) ----

CHART_W, CHART_H = 1100, 230
CM_L, CM_R, CM_T, CM_B = 64, 132, 12, 26
SERIES = (("total", "Total", "--s1"), ("premium", "Edge at sale", "--s2"), ("trading", "Trading", "--s3"))


def _nice_step(span: float, n: int = 4) -> float:
    raw = max(span, 1e-9) / n
    mag = 10 ** math.floor(math.log10(raw))
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw - 1e-12)


def _y_ticks(lo: float, hi: float):
    st = _nice_step(hi - lo)
    v, out = math.floor(lo / st) * st, []
    while v <= hi + st * 1e-6:
        out.append(round(v, 6))
        v += st
    return out


def _x_ticks(t0: float, t1: float):
    span = max(t1 - t0, 60.0)
    st = next(s for s in (900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800) if span / s <= 7)
    off = et(t0).utcoffset() if et(t0).utcoffset() is not None else timedelta(0)
    loc = t0 + off.total_seconds()
    first = (loc // st + 1) * st - off.total_seconds()
    out, v = [], first
    while v <= t1:
        out.append(v)
        v += st
    return out, st


def _x_label(ts: float, st: float) -> str:
    d = et(ts)
    s = d.strftime("%I:%M %p").lstrip("0")
    return (d.strftime("%a ") + s) if (st >= 21600 or (d.hour == 0 and d.minute == 0)) else s


def _money(v: float, signed_: bool = True) -> str:
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed_ else ("-" if v < 0 else "")
    return f"{sign}${abs(v):,.2f}"


def _axes(t0, t1, lo, hi, X, Y, money_ticks=True) -> list:
    g = []
    for v in _y_ticks(lo, hi):
        y = Y(v)
        g.append(f"<line class='grid' x1='{CM_L}' x2='{CHART_W - CM_R}' y1='{y:.1f}' y2='{y:.1f}'/>")
        g.append(f"<text class='tick' x='{CM_L - 8}' y='{y + 4:.1f}' text-anchor='end'>{html.escape(_money(v, False) if money_ticks else f'{v:g}')}</text>")
    if lo < 0 < hi:
        g.append(f"<line class='zero' x1='{CM_L}' x2='{CHART_W - CM_R}' y1='{Y(0):.1f}' y2='{Y(0):.1f}'/>")
    xs, st = _x_ticks(t0, t1)
    for t in xs:
        x = X(t)
        g.append(f"<text class='tick' x='{x:.1f}' y='{CHART_H - 8}' text-anchor='middle'>{html.escape(_x_label(t, st))}</text>")
    g.append(f"<line class='axis' x1='{CM_L}' x2='{CHART_W - CM_R}' y1='{CHART_H - CM_B}' y2='{CHART_H - CM_B}'/>")
    return g


def chart_pnl(points: list) -> str:
    """Total, edge at sale and trading over time: one $ axis, a legend with line keys,
    end labels (moved apart with a leader when they would collide), a crosshair
    tooltip, and a table view below."""
    if len(points) < 2:
        return "<div class='empty'>The P&amp;L series starts with the first fill.</div>"
    t0, t1 = points[0]["t"], points[-1]["t"]
    vals = [p[k] for p in points for k, _, _ in SERIES] + [0.0]
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or 1.0
    lo, hi = lo - pad, hi + pad
    ticks = _y_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    X = lambda t: CM_L + (t - t0) / max(t1 - t0, 1.0) * (CHART_W - CM_L - CM_R)       # noqa: E731
    Y = lambda v: CM_T + (hi - v) / max(hi - lo, 1e-9) * (CHART_H - CM_T - CM_B)      # noqa: E731
    g = [f"<svg id='pnl-svg' class='viz' viewBox='0 0 {CHART_W} {CHART_H}' role='img' "
         f"aria-label='P&amp;L over time: total, edge at sale and trading'>"]
    g += _axes(t0, t1, lo, hi, X, Y)
    ends = []
    for key, label, var in reversed(SERIES):                # Total drawn last, on top
        d = " ".join(f"{'M' if i == 0 else 'L'}{X(p['t']):.1f},{Y(p[key]):.1f}" for i, p in enumerate(points))
        g.append(f"<path d='{d}' fill='none' stroke='var({var})' stroke-width='2' stroke-linejoin='round' "
                 f"stroke-linecap='round'/>")
        ends.append((Y(points[-1][key]), key, label, var))
    ends.sort()
    ly = []
    for y, *_ in ends:                                      # end labels at least 14 apart
        ly.append(max(y, ly[-1] + 14) if ly else y)
    shift = max(0.0, ly[-1] - (CHART_H - CM_B - 4)) if ly else 0.0
    ly = [v - shift for v in ly]
    xe = X(t1)
    for (y, key, label, var), yl in zip(ends, ly):
        g.append(f"<circle cx='{xe:.1f}' cy='{y:.1f}' r='4' fill='var({var})' class='ring'/>")
        if abs(yl - y) > 1.5:
            g.append(f"<line class='leader' x1='{xe + 5:.1f}' y1='{y:.1f}' x2='{xe + 12:.1f}' y2='{yl:.1f}'/>")
        g.append(f"<text class='endlab' x='{xe + 14:.1f}' y='{yl + 4:.1f}'>{html.escape(label)} "
                 f"{html.escape(_money(points[-1][key]))}</text>")
    g.append(f"<line id='pnl-cross' class='cross' x1='0' x2='0' y1='{CM_T}' y2='{CHART_H - CM_B}' visibility='hidden'/>")
    g.append(f"<rect class='hit' x='{CM_L}' y='{CM_T}' width='{CHART_W - CM_L - CM_R}' height='{CHART_H - CM_T - CM_B}'/>")
    g.append("</svg>")
    data = [{"x": round(X(p["t"]), 1), "when": tm(p["t"]), **{k: round(p[k], 2) for k, _, _ in SERIES},
             "fees": round(p["fees"], 2)} for p in points]
    legend = "".join(f"<span><i class='key' style='background:var({var})'></i>{html.escape(label)}</span>"
                     for _, label, var in SERIES)
    rows = "".join(f"<tr><td class='small'>{tm(p['t'])}</td>" + "".join(
        f"<td class='n'>{signed(p[k])}</td>" for k, _, _ in SERIES) + f"<td class='n'>{signed(-p['fees'])}</td></tr>"
        for p in points[::max(1, len(points) // 48)] + ([points[-1]] if len(points) > 48 else []))
    return (f"<div class='legend small'>{legend}<span class='muted'>fees are the gap: total = edge at sale + "
            f"trading &minus; fees</span></div><div class='vizbox'>{''.join(g)}</div>"
            f"<script type='application/json' id='pnl-data'>{json.dumps(data)}</script>"
            f"<details class='small'><summary>P&amp;L series as a table</summary><div class='tw'><table><tr><th>ET</th>"
            + "".join(f"<th class='n'>{html.escape(label)}</th>" for _, label, _ in SERIES)
            + f"<th class='n'>Fees</th></tr>{rows}</table></div></details>")


def chart_fills(fills: list, t0: float, t1: float) -> str:
    """One dot per fill: when (x), what we collected (y); tooltip per dot. The
    fills table further down is its table view."""
    if not fills:
        return "<div class='empty'>No fills yet.</div>"
    t0 = min(t0, fills[0]["t"])
    hi = max(f["premium"] for f in fills) * 1.12 or 1.0
    ticks = _y_ticks(0.0, hi)
    hi = max(hi, ticks[-1])
    X = lambda t: CM_L + (t - t0) / max(t1 - t0, 1.0) * (CHART_W - CM_L - CM_R)       # noqa: E731
    Y = lambda v: CM_T + (hi - v) / max(hi, 1e-9) * (CHART_H - CM_T - CM_B)           # noqa: E731
    g = [f"<svg id='fill-svg' class='viz' viewBox='0 0 {CHART_W} {CHART_H}' role='img' "
         f"aria-label='Fills over time: what we collected per fill'>"]
    g += _axes(t0, t1, 0.0, hi, X, Y)
    data = []
    for i, f in enumerate(fills):
        x, y = X(f["t"]), Y(f["premium"])
        g.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='4.5' fill='var(--s1)' class='ring'/>")
        g.append(f"<circle cx='{x:.1f}' cy='{y:.1f}' r='12' class='dothit' data-i='{i}' tabindex='0'/>")
        data.append({"when": tm(f["t"]), "premium": round(f["premium"], 2), "lose": round(f["lose"], 2),
                     "ask": round(f["ask"] * 100, 1), "contracts": round(f["contracts"], 2),
                     "legs": f["legs"], "state": ("settled " + str(f.get("result") or "").upper())
                     if f.get("status") == "settled" else "open"})
    g.append("</svg>")
    return (f"<div class='vizbox'>{''.join(g)}</div>"
            f"<script type='application/json' id='fill-data'>{json.dumps(data)}</script>")


CHART_CSS = """
.viz-root,:root{--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--grid:#e1e0d9;--axisc:#c3c2b7}
@media (prefers-color-scheme:dark){:root{--s1:#3987e5;--s2:#d95926;--s3:#199e70;--grid:#2c2c2a;--axisc:#383835}}
.vizbox{position:relative;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 4px}
svg.viz{display:block;width:100%;height:auto;font:12px system-ui,Segoe UI,Arial,sans-serif}
svg.viz .grid{stroke:var(--grid);stroke-width:1}svg.viz .zero,svg.viz .axis{stroke:var(--axisc);stroke-width:1}
svg.viz .tick{fill:var(--muted);font-variant-numeric:tabular-nums}
svg.viz .endlab{fill:var(--ink);font-weight:600}svg.viz .leader{stroke:var(--muted);stroke-width:1}
svg.viz .ring{stroke:var(--card);stroke-width:2}svg.viz .cross{stroke:var(--muted);stroke-width:1}
svg.viz .hit{fill:transparent;cursor:crosshair}svg.viz .dothit{fill:transparent;cursor:pointer}
svg.viz .dothit:focus{outline:none;stroke:var(--ink);stroke-width:1}
.legend{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin:2px 0 6px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.key{display:inline-block;width:14px;height:2px;border-radius:1px}
#viz-tip{position:fixed;pointer-events:none;z-index:9;background:var(--card);color:var(--ink);border:1px solid var(--line);
border-radius:8px;padding:7px 9px;font-size:12px;box-shadow:0 2px 10px rgba(0,0,0,.12);display:none;max-width:360px}
#viz-tip .row{display:flex;align-items:center;gap:6px}#viz-tip b{font-variant-numeric:tabular-nums}
#viz-tip .when{color:var(--muted);margin-bottom:3px}
details{margin-top:6px}summary{cursor:pointer;color:var(--muted)}
"""

CHART_JS = """
<div id='viz-tip'></div>
<script>
(function () {
  var tip = document.getElementById('viz-tip');
  function money(v) { return (v > 0 ? '+' : v < 0 ? '\\u2212' : '') + '$' + Math.abs(v).toFixed(2); }
  function clear() { while (tip.firstChild) tip.removeChild(tip.firstChild); }
  function line(text, cls) { var d = document.createElement('div'); if (cls) d.className = cls; d.textContent = text; tip.appendChild(d); return d; }
  function row(val, label, color) {
    var d = document.createElement('div'); d.className = 'row';
    if (color) { var k = document.createElement('i'); k.className = 'key'; k.style.background = 'var(' + color + ')'; d.appendChild(k); }
    var b = document.createElement('b'); b.textContent = val; d.appendChild(b);
    var s = document.createElement('span'); s.className = 'muted'; s.textContent = label; d.appendChild(s);
    tip.appendChild(d);
  }
  function place(ev) {
    tip.style.display = 'block';
    var x = ev.clientX + 14, y = ev.clientY + 14;
    if (x + tip.offsetWidth > window.innerWidth - 8) x = ev.clientX - tip.offsetWidth - 14;
    if (y + tip.offsetHeight > window.innerHeight - 8) y = ev.clientY - tip.offsetHeight - 14;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  var ps = document.getElementById('pnl-svg'), pd = document.getElementById('pnl-data');
  if (ps && pd) {
    var pts = JSON.parse(pd.textContent), cross = document.getElementById('pnl-cross');
    var hit = ps.querySelector('.hit');
    function onMove(ev) {
      var m = ps.getScreenCTM(); if (!m) return;
      var p = ps.createSVGPoint(); p.x = ev.clientX; p.y = ev.clientY;
      var vx = p.matrixTransform(m.inverse()).x, best = 0;
      for (var i = 1; i < pts.length; i++) if (Math.abs(pts[i].x - vx) < Math.abs(pts[best].x - vx)) best = i;
      var q = pts[best];
      cross.setAttribute('x1', q.x); cross.setAttribute('x2', q.x); cross.setAttribute('visibility', 'visible');
      clear(); line(q.when + ' ET', 'when');
      row(money(q.total), 'Total', '--s1'); row(money(q.premium), 'Edge at sale', '--s2');
      row(money(q.trading), 'Trading', '--s3'); row(money(-q.fees), 'Fees', null);
      place(ev);
    }
    hit.addEventListener('pointermove', onMove);
    hit.addEventListener('pointerleave', function () { tip.style.display = 'none'; cross.setAttribute('visibility', 'hidden'); });
  }
  var fs = document.getElementById('fill-svg'), fd = document.getElementById('fill-data');
  if (fs && fd) {
    var fills = JSON.parse(fd.textContent);
    function show(el, ev) {
      var f = fills[+el.getAttribute('data-i')]; if (!f) return;
      clear(); line(f.when + ' ET \\u00b7 ' + f.state, 'when');
      row('$' + f.premium.toFixed(2), 'collected', null);
      row(f.ask.toFixed(1) + '\\u00a2 x ' + f.contracts, 'our price x contracts', null);
      row('$' + f.lose.toFixed(2), 'can lose', null);
      line(f.legs);
      if (ev && ev.clientX !== undefined) { place(ev); }
      else { var r = el.getBoundingClientRect(); place({clientX: r.right, clientY: r.bottom}); }
    }
    Array.prototype.forEach.call(fs.querySelectorAll('.dothit'), function (el) {
      el.addEventListener('pointermove', function (ev) { show(el, ev); });
      el.addEventListener('pointerleave', function () { tip.style.display = 'none'; });
      el.addEventListener('focus', function () { show(el, null); });
      el.addEventListener('blur', function () { tip.style.display = 'none'; });
    });
  }
})();
</script>
"""


SEG_CSS = """
.segtabs{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0 10px}
.segtab{border:1px solid var(--line);background:var(--card);color:var(--ink);border-radius:999px;padding:3px 12px;
font:inherit;font-size:12px;cursor:pointer}.segtab.on{background:var(--accent);border-color:var(--accent);color:#fff}
.seg h3{font-size:13px;font-weight:600;margin:12px 0 6px}
"""

SEG_JS = """<script>(function () {
  var KEY = 'combo-dash-segment', tabs = [].slice.call(document.querySelectorAll('.segtab')),
      segs = [].slice.call(document.querySelectorAll('.seg'));
  if (!tabs.length || !segs.length) return;
  function show(d) {
    if (!segs.some(function (s) { return s.getAttribute('data-dim') === d; })) d = segs[0].getAttribute('data-dim');
    segs.forEach(function (s) { s.style.display = s.getAttribute('data-dim') === d ? '' : 'none'; });
    tabs.forEach(function (t) { t.classList.toggle('on', t.getAttribute('data-dim') === d); });
    try { localStorage.setItem(KEY, d); } catch (e) {}
  }
  var d0 = null;
  try { d0 = localStorage.getItem(KEY); } catch (e) {}
  tabs.forEach(function (t) { t.addEventListener('click', function () { show(t.getAttribute('data-dim')); }); });
  show(d0 || 'legs');
})();</script>"""


def _seg_label(dim: str, v) -> str:
    return f"{v} legs" if dim == "legs" and isinstance(v, int) else html.escape(str(v))


def _fill_rate_cell(filled: int, quotes: int) -> str:
    if not quotes:
        return "&ndash;"
    if not filled:
        return "0"
    return f"{100.0 * filled / quotes:.3f}% &middot; 1 in {quotes / filled:,.0f}"


def analytics_html(an: dict, s_outcomes=None) -> str:
    """The segment tables, one dimension shown at a time (the choice kept in the
    browser), each with the quoting since the log's first quote and the P&L of
    every filled ticket, and a total row that matches the cards above."""
    if not an or not an.get("dims"):
        return ""
    since = an.get("since")
    since_s = (et(since).strftime("%a %I:%M %p").replace(" 0", " ") + " ET") if since else "the log's start"
    tabs = "".join(f"<button type='button' class='segtab' data-dim='{d}'>{html.escape(lab)}</button>"
                   for d, lab in DIMS)
    H = ["<h2>Analytics <span class='muted small'>by segment</span></h2>", f"<div class='segtabs'>{tabs}</div>"]
    for dim, label in DIMS:
        rows = an["dims"].get(dim) or []
        qrows = [r for r in rows if r["quotes"] or r["accepted"] or r["filled"]]
        trows = [r for r in rows if r["tickets"]]
        tot = {k: sum(r[k] for r in rows) for k in rows[0] if k != "seg"} if rows else {}
        H.append(f"<div class='seg' data-dim='{dim}'>")
        H.append(f"<h3>Quoting and fills since {html.escape(since_s)}</h3><div class='tw'><table><tr>"
                 f"<th>{html.escape(label)}</th><th class='n'>Quotes</th><th class='n'>Edge asked</th>"
                 "<th class='n'>Accepted</th><th class='n'>Declined at the last look</th><th class='n'>Filled</th>"
                 "<th class='n'>Fill rate</th></tr>")
        for r, cls in [(r, "") for r in qrows] + ([(tot, " class='tot'")] if qrows else []):
            name = "Total" if r is tot else _seg_label(dim, r["seg"])
            asked = f"{100.0 * r['mu_sum'] / r['quotes']:.1f}%" if r["quotes"] else "&ndash;"
            H.append(f"<tr{cls}><td>{name}</td><td class='n'>{r['quotes']:,}</td><td class='n'>{asked}</td>"
                     f"<td class='n'>{r['accepted']:,}</td><td class='n'>{r['declined']:,}</td>"
                     f"<td class='n'>{r['filled']:,}</td><td class='n'>{_fill_rate_cell(r['filled'], r['quotes'])}</td></tr>")
        if not qrows:
            H.append("<tr><td colspan='7' class='empty'>No quotes in the log yet.</td></tr>")
        H.append("</table></div>")
        H.append("<h3>Tickets and P&amp;L, every fill</h3><div class='tw'><table><tr>"
                 f"<th>{html.escape(label)}</th><th class='n'>Tickets</th><th class='n'>Collected</th>"
                 "<th class='n'>Avg price</th><th class='n'>Edge at sale</th><th class='n'>Trading</th>"
                 "<th class='n'>Fees</th><th class='n'>P&amp;L</th><th class='n'>Settled: hit vs fair</th>"
                 "<th class='n'>Open: can lose</th></tr>")
        for r, cls in [(r, "") for r in trows] + ([(tot, " class='tot'")] if trows else []):
            name = "Total" if r is tot else _seg_label(dim, r["seg"])
            avg = f"{100.0 * r['collected'] / r['contracts']:.1f}&cent;" if r["contracts"] else "&ndash;"
            share = (f" <span class='muted small'>{100.0 * r['edge'] / r['collected']:.1f}%</span>"
                     if r["collected"] else "")
            hit = (f"{r['hits']} hit / {r['exp_hits']:.1f} expected of {r['settled']}" if r["settled"]
                   else "&ndash;")
            H.append(f"<tr{cls}><td>{name}</td><td class='n'>{r['tickets']:,}</td><td class='n'>{usd(r['collected'])}</td>"
                     f"<td class='n'>{avg}</td><td class='n'>{signed(r['edge'])}{share}</td>"
                     f"<td class='n'>{signed(r['trading'])}</td><td class='n'>{signed(-r['fees'])}</td>"
                     f"<td class='n'>{signed(r['pnl'])}</td><td class='n'>{hit}</td>"
                     f"<td class='n'>{usd(r['can_lose']) if r['open'] else '&ndash;'}</td></tr>")
        if not trows:
            H.append("<tr><td colspan='10' class='empty'>No fills yet.</td></tr>")
        H.append("</table></div>")
        orows = [r for r in rows if r.get("checked")]
        H.append(f"<h3>What became of our quotes <span class='muted small'>a {OUTCOME_SAMPLE_PCT}% sample the "
                 "buyer did not accept, read off each combo's trade tape</span></h3><div class='tw'><table><tr>"
                 f"<th>{html.escape(label)}</th><th class='n'>Checked</th><th class='n'>Not bought</th>"
                 "<th class='n'>Bought cheaper elsewhere</th><th class='n'>At our price elsewhere</th>"
                 "<th class='n'>Dearer elsewhere</th><th class='n'>Another request&rsquo;s print</th>"
                 "<th class='n'>Cheaper by (avg)</th></tr>")
        for r, cls in [(r, "") for r in orows] + ([(tot, " class='tot'")] if orows else []):
            name = "Total" if r is tot else _seg_label(dim, r["seg"])
            n = r["checked"]
            cells = "".join(f"<td class='n'>{100.0 * r[k] / n:.1f}% <span class='muted small'>{r[k]:,}</span></td>"
                            for _o, k in OUTCOME_KINDS)
            gap = f"{100.0 * r['gap_cheaper'] / r['o_cheaper']:+.1f}%" if r["o_cheaper"] else "&ndash;"
            H.append(f"<tr{cls}><td>{name}</td><td class='n'>{n:,}</td>{cells}<td class='n'>{gap}</td></tr>")
        if not orows:
            H.append("<tr><td colspan='8' class='empty'>No quotes checked yet (the first are read 2 minutes after "
                     "their RFQ).</td></tr>")
        H.append("</table></div></div>")
    oc = s_outcomes
    if oc:
        H.append(f"<div class='small muted'>Quote outcomes: {oc['checked']:,} checked, {oc['queue']:,} waiting, "
                 f"{oc['calls']:,} trade reads since the page started"
                 + (f" &middot; <span class='neg'>{html.escape(str(oc['error']))}</span>" if oc.get("error") else "")
                 + ". Not bought: no print of the request's size on the combo within 2 minutes. Cheaper / at our "
                 "price / dearer elsewhere: another maker's fill of the request, its price against ours. Another "
                 "request's print: the same parlay traded for a request of another size (or NO was bought), and ours "
                 "did not -- every request for one set of legs shares a combo.</div>")
    H.append("<div class='small muted'>Edge asked: the average markup we quoted. Fill rate: fills / quotes. Edge at "
             "sale: our price over the fair when we sold; trading: the parlays' moves since (payouts once settled); "
             "P&amp;L = edge at sale + trading &minus; fees. Hit vs fair: settled parlays that hit (we paid) against "
             "the sum of their fairs at the sale.</div>")
    return "".join(H)


def render(s: dict) -> str:
    cfg = s["cfg"]
    status = ("live" if s["live"] else "dry") if s["running"] else "off"
    label = {"live": "LIVE", "dry": "DRY", "off": "STOPPED" if s["stopped"] else "NOT RUNNING"}[status]
    games = ", ".join(cfg.get("games") or []) or "any"
    mu, step = (_f(cfg.get('markup'), 0) or 0), (_f(cfg.get('markup_per_leg'), 0) or 0)
    cut = _f(cfg.get('nfl_markup_cut'), 0) or 0
    floor = _f(cfg.get('confirm_edge_floor'), 0) or 0
    fstep = _f(cfg.get('confirm_floor_per_leg'), 0) or 0
    longs = sorted(((_f(x[0], 0) or 0), (_f(x[1], 0) or 0)) for x in cfg.get('longshot_markup') or [] if len(x) == 2)
    setting = (f"markup {mu * 100:.3g}%" + (f" +{step * 100:.3g}%/leg over 2" if step else "")
               + (f", all-NFL {cut * 100:.3g} pts less" if cut else "")
               + ((", long shots " + ", ".join(f"+{e * 100:.3g} under {t * 100:.3g}&cent;" for t, e in reversed(longs)))
                  if longs else "")
               + (f", last look &ge; {floor * 100:.3g}%" + (f" +{fstep * 100:.3g}%/leg" if fstep else "")
                  + " after fees" if floor else "")
               + f" &middot; {cfg.get('min_legs', '?')}&ndash;"
               f"{cfg.get('max_legs', '?')} legs &middot; games {html.escape(games)} &middot; "
               f"lead {cfg.get('min_lead_min', '?')} min &middot; "
               + (f"settles &le; {cfg.get('max_days_out'):g} days &middot; " if cfg.get("max_days_out") else "")
               + (f"loss ${cfg.get('max_ticket_loss'):,.0f}/ticket &middot; " if cfg.get("max_ticket_loss") else "")
               + f"caps ${cfg.get('max_payout', '?')}/ticket, "
               f"${cfg.get('max_game_payout', '?')}/game, open loss ${cfg.get('max_open_loss', '?')}") if cfg else "no run yet"
    k = s.get("kalshi") or {}
    fn = s["funnel"]
    qn = fn["quotes"] or fn["would"]
    H = [f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
         f"<meta http-equiv='refresh' content='{REFRESH_S}'><title>Combo maker</title><style>{CSS}{CHART_CSS}{SEG_CSS}</style></head><body><main>"]
    H.append(f"<div class='top'><div><h1>Combo maker <span class='pill {status}'>{label}</span></h1>"
             f"<div class='muted small'>{setting}</div></div>"
             f"<div class='muted small'>as of {tm(s['now'])} ET &middot; refreshes every {REFRESH_S}s"
             + (f" &middot; last line {s['last_line_age']:.0f}s ago" if s.get("last_line_age") is not None else "")
             + "</div></div>")
    H.append("<div class='cards'>")
    H.append(f"<div class='card'><div class='l'>Combo shard cash</div><div class='v'>{usd(k.get('cash'))}</div>"
             f"<div class='l'>{'Kalshi ' + tm(k['at']) if k.get('at') else 'not read yet'}</div></div>")
    H.append(f"<div class='card'><div class='l'>Open tickets</div><div class='v'>{len(s['open'])}</div>"
             f"<div class='l'>can lose {usd(s['open_loss'])} &middot; collected {usd(s['open_premium'])}</div></div>")
    H.append(f"<div class='card'><div class='l'>Fees on combo fills</div><div class='v'>{usd(s['fees'])}</div>"
             f"<div class='l'>{len(s['fills'])} fills on the combo shard, Kalshi's record</div></div>")
    fr = s.get("fill_rate") or {}
    if fr.get("quotes"):
        one_in = f" &middot; 1 in {fr['quotes'] / fr['filled']:,.0f}" if fr.get("filled") else ""
        since = et(fr["since"]).strftime("%a %I:%M %p").replace(" 0", " ")
        H.append(f"<div class='card'><div class='l'>Quotes filled</div>"
                 f"<div class='v'>{100.0 * fr['filled'] / fr['quotes']:.3f}%</div>"
                 f"<div class='l'>{fr['filled']} fills of {fr['quotes']:,} quotes since {html.escape(since)}{one_in}"
                 f" &middot; {100.0 * fr['accepted'] / fr['quotes']:.3f}% accepted"
                 + (f" &middot; <span class='neg'>{fr['taker']} filled as taker</span>" if fr.get("taker") else "")
                 + "</div></div>")
    H.append("</div>")
    pl = s.get("pnl") or {}
    if pl:
        H.append("<h2>P&amp;L</h2><div class='cards'>")
        H.append(f"<div class='card'><div class='l'>Total</div><div class='v'>{signed(pl.get('total'))}</div>"
                 f"<div class='l'>settled + unrealized</div></div>")
        H.append(f"<div class='card'><div class='l'>Settled</div><div class='v'>{signed(pl.get('settled'))}</div>"
                 f"<div class='l'>{s['wins']} kept &middot; {s['losses']} paid out</div></div>")
        H.append(f"<div class='card'><div class='l'>Unrealized</div><div class='v'>{signed(pl.get('unrealized'))}</div>"
                 f"<div class='l'>{len(s['open'])} open at the legs' mids now"
                 + (f" &middot; {pl['unpriced']} unpriced" if pl.get("unpriced") else "") + "</div></div>")
        H.append(f"<div class='card'><div class='l'>Edge at sale</div><div class='v'>{signed(pl.get('premium'))}</div>"
                 f"<div class='l'>our price over the fair when we sold, on {usd(pl.get('collected'))} collected</div></div>")
        H.append(f"<div class='card'><div class='l'>Trading</div><div class='v'>{signed(pl.get('trading'))}</div>"
                 f"<div class='l'>the parlays' moves since the sale (payouts once settled)</div></div>")
        H.append(f"<div class='card'><div class='l'>Fees</div><div class='v'>{signed(-(pl.get('fees') or 0.0))}</div>"
                 f"<div class='l'>total = edge at sale + trading &minus; fees</div></div>")
        H.append("</div>")
    fp = s.get("fill_points") or []
    if fp:
        ser = s.get("series") or []
        H.append("<h2>P&amp;L over time</h2>" + chart_pnl(ser))
        H.append(f"<h2>Fills over time <span class='muted small'>{len(fp)} fills, "
                 f"{usd(sum(f['premium'] for f in fp))} collected; a dot is one fill, its height what we collected"
                 f"</span></h2>" + chart_fills(fp, (ser[0]['t'] if ser else fp[0]['t']), s['now']))
        if s.get("history_error"):
            H.append(f"<div class='small muted'>price history: {html.escape(str(s['history_error']))}</div>")
    H.append(analytics_html(s.get("analytics"), s.get("outcomes")))
    H.append("<h2>Today</h2><div class='card'><div class='funnel'>"
             f"<b>{qn:,}</b> {'quotes sent' if fn['quotes'] else 'would quote (dry)'} <span class='arrow'>&rarr;</span> "
             f"<b>{fn['accepted']:,}</b> accepted <span class='arrow'>&rarr;</span> <b>{fn['confirmed']:,}</b> confirmed "
             f"<span class='arrow'>&rarr;</span> <b>{fn['executed']:,}</b> filled"
             + (f" ({fn['unfilled']} canceled by Kalshi, 0 filled)" if fn.get("unfilled") else "")
             + f"<span class='muted'>&nbsp;&middot; {fn['declined']} declined at the last look &middot; "
             f"{fn['errors']} errors</span></div>")
    if s["declines"]:
        H.append("<div class='chips small' style='margin-top:6px'>Declined because: " + "".join(
            f"<span>{html.escape(str(w))} {n}</span>" for w, n in sorted(s["declines"].items(), key=lambda kv: -kv[1])) + "</div>")
    ls = s.get("last_stat")
    if ls:
        top = sorted((ls.get("why") or {}).items(), key=lambda kv: -kv[1])[:6]
        H.append(f"<div class='small muted' style='margin-top:6px'>Last minute: {ls.get('rfq', 0):,} RFQs seen "
                 f"&rarr; {ls.get('pre_ok', 0):,} pass the leg filters &rarr; {ls.get('priced', 0):,} priced &rarr; "
                 f"{ls.get('quoted', ls.get('dry', 0)) or ls.get('dry', 0):,} quoted. Skipped: "
                 + ", ".join(f"{html.escape(w)} {n:,}" for w, n in top) + "</div>")
    p = s.get("pulse")
    if p and p.get("secs"):
        H.append(f"<div class='small muted'>The whole market, last minute: ~{p.get('rfq', 0) * (p.get('shard') or [50])[0] / p['secs']:,.0f} "
                 f"RFQs/s, {p.get('mve_prints', 0) / p['secs']:.0f} combo fills/s, "
                 f"takers paid {usd(p.get('mve_usd', 0) / p['secs'] * 60, 0)} a minute.</div>")
    H.append("</div>")
    H.append(f"<h2>Open tickets ({len(s['open'])})</h2><div class='tw'>")
    if s["open"]:
        H.append("<table><tr><th>Confirmed</th><th>Legs (we sold the parlay)</th><th class='n'>Our price</th>"
                 "<th class='n'>Fair (quote &rarr; confirm)</th><th class='n'>Contracts</th><th class='n'>Collected</th>"
                 "<th class='n'>Can lose</th><th class='n'>Fair now</th><th class='n'>P&amp;L now</th><th>Status</th></tr>")
        mk = k.get("markets") or {}
        for t in sorted(s["open"], key=lambda t: -t.get("at", 0)):
            m = mk.get(t["mt"]) or {}
            st = t["status"] + (f" &middot; Kalshi {html.escape(str(m.get('status')))}" if m else "")
            if m.get("result"):
                st += f" &middot; result {html.escape(str(m['result']).upper())}"
            fc = t.get("fair_confirm")
            H.append(f"<tr><td class='small'>{tm(t.get('at', 0))}</td><td class='small'>{legs_html(t['legs'])}</td>"
                     f"<td class='n'>{t['ask'] * 100:.1f}&cent;</td>"
                     f"<td class='n'>{t['fair'] * 100:.1f} &rarr; {(fc * 100 if fc else 0):.1f}&cent;</td>"
                     f"<td class='n'>{t['contracts']:,.1f}</td><td class='n'>{usd(t['contracts'] * t['ask'])}</td>"
                     f"<td class='n'>{usd(t['contracts'] * (1 - t['ask']))}</td>"
                     f"<td class='n'>{(t['_value'] * 100 if t.get('_value') is not None else 0):.1f}&cent;</td>"
                     f"<td class='n'>{signed(t.get('_pnl'))}</td><td class='small'>{st}</td></tr>")
        # the column sums (Jack 2026-11-06: the rows' P&L now + the settled P&L "doesnt add up to the
        # total"; it does at one refresh, but the two in-play tennis tickets at the bottom carried
        # nearly all of it and move between refreshes)
        n = len(s["open"])
        H.append(f"<tr class='tot'><td colspan='5'>Total of {n} &middot; the Unrealized card</td>"
                 f"<td class='n'>{usd(sum(t['contracts'] * t['ask'] for t in s['open']))}</td>"
                 f"<td class='n'>{usd(sum(t['contracts'] * (1 - t['ask']) for t in s['open']))}</td><td></td>"
                 f"<td class='n'>{signed(sum(_f(t.get('_pnl'), 0.0) for t in s['open']))}</td><td></td></tr>")
        H.append("</table>")
    else:
        H.append("<div class='empty'>No open tickets.</div>")
    H.append(f"</div><h2>Settled ({len(s['settled'])})</h2><div class='tw'>")
    if s["settled"]:
        H.append("<table><tr><th>Confirmed</th><th>Legs</th><th class='n'>Our price</th><th class='n'>Contracts</th>"
                 "<th>Parlay</th><th class='n'>P&amp;L</th></tr>")
        for t in sorted(s["settled"], key=lambda t: -t.get("at", 0)):
            res = "hit (we paid)" if t.get("result") == "yes" else "missed (we kept it)"
            H.append(f"<tr><td class='small'>{tm(t.get('at', 0))}</td><td class='small'>{legs_html(t['legs'])}</td>"
                     f"<td class='n'>{t['ask'] * 100:.1f}&cent;</td><td class='n'>{t['contracts']:,.1f}</td>"
                     f"<td>{res}</td><td class='n'>{signed(_f(t.get('pnl')))}</td></tr>")
        H.append(f"<tr class='tot'><td colspan='5'>Total of {len(s['settled'])} &middot; the Settled card</td>"
                 f"<td class='n'>{signed(sum(_f(t.get('pnl'), 0.0) for t in s['settled']))}</td></tr>")
        H.append("</table>")
    else:
        H.append("<div class='empty'>Nothing settled yet.</div>")
    H.append("</div>")
    if s["fills"]:
        H.append(f"<h2>Fills on Kalshi ({len(s['fills'])})</h2><div class='tw'><table><tr><th>Time</th><th>Combo</th>"
                 "<th>Side</th><th class='n'>Contracts</th><th class='n'>YES price</th><th class='n'>Fee</th></tr>")
        for x in sorted(s["fills"], key=lambda x: str(x.get("created_time")), reverse=True)[:30]:
            H.append(f"<tr><td class='small'>{html.escape(str(x.get('created_time', ''))[11:19])}Z</td>"
                     f"<td class='small'>&hellip;{html.escape(str(x.get('market_ticker') or x.get('ticker'))[-10:])}</td>"
                     f"<td>{html.escape(str(x.get('outcome_side') or x.get('side') or ''))}</td>"
                     f"<td class='n'>{_f(x.get('count_fp'), 0):,.2f}</td>"
                     f"<td class='n'>{(_f(x.get('yes_price_dollars'), 0) or 0) * 100:.2f}&cent;</td>"
                     f"<td class='n'>{usd(_f(x.get('fee_cost'), 0), 4)}</td></tr>")
        H.append("</table></div>")
    H.append("<h2>Recent activity</h2><div class='tw'><table><tr><th>Time</th><th>What</th><th>Detail</th></tr>")
    for e in s["feed"]:
        kind = e.get("k")
        when = tm(_ev_ts(e)) if _ev_ts(e) else ""
        if kind in ("quote", "would_quote"):
            d = (f"{len(e.get('legs') or [])} legs &middot; sell at {e['ask'] * 100:.1f}&cent; (fair {e['fair'] * 100:.1f}&cent;)"
                 f" &middot; {e.get('contracts', 0):,.0f} contracts &middot; {usd(e.get('contracts', 0) * e['ask'])}"
                 + (f" &middot; sent in {e['sent_ms']:.0f} ms" if e.get("sent_ms") is not None else "")
                 + f"<div class='muted small'>{legs_html(e.get('legs') or [])}</div>")
        elif kind == "accept":
            d = f"&hellip;{html.escape(str(e.get('mt'))[-10:])} &middot; {_f(e.get('contracts'), 0):,.1f} contracts"
        elif kind == "confirm":
            d = (f"sold {e['contracts']:,.1f} at {e['ask'] * 100:.1f}&cent; &middot; can lose "
                 f"{usd(e['contracts'] * (1 - e['ask']))} &middot; confirmed in {e.get('confirm_ms', 0):.0f} ms")
        elif kind == "decline":
            d = f"{html.escape(str(e.get('why')))} &middot; fair then {(_f(e.get('fair_now')) or 0) * 100:.1f}&cent; vs our {e['ask'] * 100:.1f}&cent;"
        elif kind == "settled":
            d = f"result {html.escape(str(e.get('result')).upper())} &middot; P&amp;L {signed(_f(e.get('pnl')))}"
        elif kind == "err":
            d = f"<span class='neg'>{html.escape(str(e.get('what', '')))}: {html.escape(str(e.get('err') or e.get('msg'))[:200])}</span>"
        elif kind == "position":
            d = f"position read back: {e.get('position')} (negative = NO, as intended)"
        elif kind == "start":
            d = "started " + ("LIVE" if e.get("live") else "DRY")
        else:
            d = html.escape(json.dumps({x: e[x] for x in e if x not in ("k",)})[:160])
        label = {"would_quote": "would quote"}.get(kind, kind)
        H.append(f"<tr><td class='small'>{when}</td><td><span class='k {kind}'>{label}</span></td><td>{d}</td></tr>")
    if not s["feed"]:
        H.append("<tr><td colspan='3' class='empty'>No maker activity yet.</td></tr>")
    H.append("</table></div>")
    if k.get("error"):
        H.append(f"<footer class='neg'>Kalshi read failed: {html.escape(k['error'])}</footer>")
    H.append(f"<footer>Read-only: the maker's files in {html.escape(MAKER_DIR)} and, once a minute, Kalshi's balance, "
             "fills and markets. Times are ET.</footer></main>" + CHART_JS + SEG_JS + "</body></html>")
    return "".join(H)


def make_handler(maker_dir, shadow_dir, kalshi, history=None, logs=None, outcomes=None):
    logs = logs if logs is not None else MakerLog(maker_dir)       # kept across requests

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                s = snapshot(maker_dir, shadow_dir, time.time(), kalshi, history, logs, outcomes)
                if self.path.startswith("/data.json"):
                    body, ctype = json.dumps(s, default=str).encode(), "application/json"
                else:
                    body, ctype = render(s).encode("utf-8"), "text/html; charset=utf-8"
                self.send_response(200)
            except Exception as e:
                body, ctype = f"dashboard error: {type(e).__name__}: {e}".encode(), "text/plain"
                self.send_response(500)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


class DashServer(ThreadingHTTPServer):
    """One dashboard to a port. HTTPServer sets SO_REUSEADDR, and on Windows that
    lets a second process bind a port already in use -- a second dash would serve
    beside the first. There the bind is exclusive instead, so a second copy fails."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(srv, stop_file=None, check_s: float = STOP_CHECK_S) -> int:
    """Serve until Ctrl+C (0), or until the stop file appears (EXIT_STOP)."""
    seen, done = threading.Event(), threading.Event()

    def watch():
        while not done.wait(check_s):
            if stop_file and os.path.exists(stop_file):
                seen.set()
                srv.shutdown()
                return

    threading.Thread(target=watch, daemon=True).start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        done.set()
        srv.server_close()
    return EXIT_STOP if seen.is_set() else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--no-kalshi", action="store_true", help="local files only")
    a = ap.parse_args(argv)
    if os.path.exists(STOP_FILE):
        print(f"{STOP_FILE} exists: not starting (delete it and the task starts the dash again)", flush=True)
        return EXIT_STOP
    kalshi = None if a.no_kalshi else KalshiReads()
    history = None if kalshi is None else HistoryReads(kalshi)
    outcomes = None if kalshi is None else QuoteOutcomes(kalshi)
    logs = MakerLog(MAKER_DIR, sampler=outcomes)
    try:
        srv = DashServer(("127.0.0.1", a.port), make_handler(MAKER_DIR, SHADOW_DIR, kalshi, history, logs, outcomes))
    except OSError as e:
        print(f"port {a.port} is taken -- another dashboard serves it? ({e}); exiting", flush=True)
        return EXIT_PORT
    print(f"combo dashboard on http://127.0.0.1:{a.port} (Ctrl+C to stop)", flush=True)
    t = time.perf_counter()               # the cold reads (the day's log, the first Kalshi and candle reads),
    try:                                  # a page asked for meanwhile waits
        snapshot(MAKER_DIR, SHADOW_DIR, time.time(), kalshi, history, logs)
        days = logs.update(time.time())
        print(f"warmed in {time.perf_counter() - t:.1f} s: {sum(d.quotes for d in days):,} quotes, "
              f"{sum(d.off for d in days) / 1e6:,.1f} MB of log", flush=True)
    except Exception as e:                # the page shows it; serve anyway
        print(f"warm-up failed: {type(e).__name__}: {e}", flush=True)
    if outcomes is not None:
        outcomes.start()
        print(f"quote outcomes: {len(outcomes.done):,} read back, {len(outcomes.queue):,} queued", flush=True)
    return serve(srv, STOP_FILE)


if __name__ == "__main__":
    sys.exit(main())
