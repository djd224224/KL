#!/usr/bin/env python3
"""
trump_mention_fair.py -- pre-event fair values for KXTRUMPMENTION and
KXTRUMPMENTIONB, "What will Trump say during <event>?" (Jack 2026-10-25:
"weight near-term more heavily").

Each market is a word or phrase ("Save America Act", "Iran (5+ times)",
"China / Chinese") and settles YES if Trump says it during the event under
the MENTION rules (plurals and possessives count, either side of a slash
counts, "(N+ times)" needs N). One market per event, "Event does not
qualify", is YES when the event is cancelled or never qualifies, and then
every word settles NO. The bot stops quoting at the event's start (the
live latch), so this is a PRE-EVENT probability: a guard against getting
picked off, not a price we quote at.

THE DATA is Kalshi's own settled history of both series, with its rules
already applied: GET /markets?series_ticker=..&status=settled (and, cold,
/historical/markets for the archive, settled before 2026-08-26) -- 366
events and 10,013 settled markets from 4/2025 to 10/2026 (KXTRUMPMENTION
249 events, B 117; voided and scalar events left out). The word key is
custom_strike.Word lower-cased with a slash's terms sorted ("Gas /
Gasoline / Oil" == "oil / gas / gasoline"); "(3+ times)" stays part of
the key. The event type comes from the market's title and the event its
rules_primary names: rally, telerally, interview, presser, cabinet,
bilateral, address, roundtable, else remarks. Their YES rates run from 28%
(bilateral, 12 of 55 events duds) and 36% (telerally) to 53% (rally) and
62% (cabinet). Roll Call Factba.se transcripts are not used: rollcall.com's
robots.txt disallows Anthropic's agent.

THE MODEL, a two-level Beta-binomial with recency weights:
  base_T   type T's YES rate, weights 0.5^(age / HALF_LIFE_DAYS), shrunk to
           the all-type rate with TYPE_PRIOR_N pseudo-words;
  lift_k   word k's log-odds lift over the base rates of the events it was
           listed in (same weights), shrunk to 0 with WORD_PRIOR_N;
  p        word k in type-T events, weights 0.5^(age /
           HALF_LIFE_WORD_TYPE_DAYS), shrunk to logistic(logit(base_T) +
           lift_k) with WORD_TYPE_PRIOR_N:
           p = (sum w y + WORD_TYPE_PRIOR_N prior) / (sum w + WORD_TYPE_PRIOR_N)
A word never listed before gets lift 0, the type's base rate (MIN_HISTORY_N
sets whether it is priced at all). "Event does not qualify" gets the type's
recency-weighted dud rate (a dud: its own YES, or an event of 10+ words all
NO). Dud events stay in the word counts, so every p carries the dud risk.

THE HALF-LIVES, by walk-forward log loss: every event from 10/2025 priced
from the markets settled before it (8,704 words): no decay 0.636, 14 days
0.633, 30 days 0.626, 30 days on the word x type counts with 60 on the lift
and base rates 0.624 (the best; 60/60 0.625, 90 0.626); rallies alone
0.551 without decay vs 0.529 (Brier 0.189 vs 0.180). A type-only base rate
scores 0.676. So recent events count more: an event two months back counts
a quarter as much in the word's record at that event type, half as much
in its lift and the base rates.

AGAINST THE MARKET it loses as a forecaster: on 44 events 8/25-10/25
(1,283 words, hourly candles) the pre-event mid scores Brier 0.147 vs the
model's 0.194 at start - 30 min and 0.142 vs 0.169 three days out, and a
50/50 blend is worse than the mid alone; it cannot see a venue (Texas
prices "Drill Baby Drill" 75c, Mississippi 9c; one rally fair for both) or
a riff that started last week ("America First" said at the last 5 rallies
after 7 without: fair 41c vs an 87c book). So the gate is a wide band, not
a price. Its value is on OUR fills (9/26-10/25, 2,628 fills, $ to
settlement or now): a touch more than 15c through this fair (as of each
fill, from markets settled before it) held 218 of 748 YES-bid fills at
-13.4c a contract vs -5.6c for the rest, and 273 of 1,880 asks at -15.7c
vs +2.1c -- -$2,011 of fills held (-$1,445 on settled events; bids lost in
both halves of the month, asks only in the second; 22 of 30 events
helped), for ~$420 of the $2,760 of 30-day reward (each side's share of
quoting hours capped, from hourly candles). Tol 10 holds -$2,402 for
~$580; tol 20 -$1,759 for ~$295.

The file is the rain writers' shape for the bot's loader (markets: p /
event / fetched_at, plus word / type / n_eff / n_type / base diagnostics).
fetched_at is the older of the last successful settled-history read and
open-event read, so the gate's TTL stands the family aside when either
keeps failing. The file carries its own memory: the settled history,
seeded from SEED_FILE, read again every HISTORY_TTL_SECS for the markets
settled since the newest held (minus HISTORY_OVERLAP_SECS); Kalshi's open
events every EVENTS_TTL_SECS. An event with any market closed or settled
is under way or over: no fair.

Standalone dry run (writes DEFAULT_OUT in the temp folder, never the bot's
status folder, and prints each open market's fair beside its book):
    python trump_mention_fair.py [--out path] [--tol 15]
"""
import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import requests

SERIES = ("KXTRUMPMENTION", "KXTRUMPMENTIONB")
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
USER_AGENT = "KL-IMM-trump-mention-fair/1.0"
HTTP_TIMEOUT = 30
REQUEST_GAP_SECS = 0.35          # Kalshi's public API rate-limits (429s on 2026-10-25)
RETRY_SLEEPS = (2.0, 6.0, 15.0)  # on a 429 / 5xx, then give up (the read fails)
MAX_PAGES = 40                   # per paged read; more means something is wrong
HISTORY_TTL_SECS = 3600          # settled history at most hourly
EVENTS_TTL_SECS = 600            # open events every 10 minutes
HISTORY_OVERLAP_SECS = 2 * 86400  # re-read settlements this far behind the newest held
KEEP_DAYS = 365                  # history kept (weight at 365 days: 1.5%)
HALF_LIFE_WORD_TYPE_DAYS = 30.0  # the word's own record in this event type
HALF_LIFE_DAYS = 60.0            # the word's lift and the type base rates
WORD_TYPE_PRIOR_N = 3.0
WORD_PRIOR_N = 5.0
TYPE_PRIOR_N = 20.0
DUD_PRIOR_N = 10.0
P_MIN, P_MAX = 0.01, 0.99
MIN_HISTORY_N = 0.0              # leave out a word with less weighted history (0: price
                                 # it at the type's base rate, the wide prior)
EDQ_KEY = "event does not qualify"
RECOMMENDED_TOL_CENTS = 15       # the CLI's flags only; the bot's gate holds the real one
SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "trump_mention_seed.json")
DEFAULT_OUT = os.path.join(tempfile.gettempdir(), "trump_mention_fair.json")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"))}
_TRADING = ("active", "open")
_DONE = ("closed", "determined", "disputed", "amended", "finalized", "settled")
_TICKER_DAY_RE = re.compile(r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})")
_RULES_EVENT_RE = re.compile(r"as part of\s+(.*?)\s*,?\s*then the market", re.I | re.S)
_TYPES = (
    ("telerally", r"tele-?\s?rally"),
    ("rally", r"\brally\b|\brallies\b"),
    ("interview", r"interview|fox news|fox & friends|fox and friends|hannity|ingraham|"
                  r"meet the press|60 minutes|cnbc|newsmax|podcast|radio|\bshow\b|"
                  r"sunday morning futures|squawk|newsnation|bongino|america's voice|"
                  r"gb news|punchbowl|sunday night in america|my view with|fox business|"
                  r"\bcbs\b|\bnbc\b|\babc\b|\bcnn\b|town hall|mornings with maria|"
                  r"the record with|fast money|hugh hewitt|fox appearance|\btonight\b|\bwabc\b"),
    ("presser", r"press conference|news conference|presser|gaggle"),
    ("cabinet", r"cabinet meeting"),
    ("bilateral", r"bilateral|meeting with|meetings with|meets with|meet with|working lunch|"
                  r"working breakfast|\btea\b|state banquet|state dinner|state visit|"
                  r"arrival ceremony|greeting|dinner with|lunch with|summit with|visit with|"
                  r"hosted by the (?:president|prime minister|king)"),
    ("address", r"address to the nation|speech to the nation|state of the union|"
                r"joint session|general assembly|\bunga\b|commencement|keynote|address at|"
                r"address to|convention|\bcpac\b|prayer breakfast|correspondents"),
    ("roundtable", r"roundtable|round table"),
)
_TYPE_RES = tuple((t, re.compile(rx, re.I)) for t, rx in _TYPES)
_last_request = [0.0]


def _log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} [trump-mention] {msg}", flush=True)


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            js = json.load(f)
        return js if isinstance(js, dict) else {}
    except (OSError, ValueError):
        return {}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(s) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _cents(v) -> Optional[float]:
    try:
        return round(100.0 * float(v), 1)
    except (TypeError, ValueError):
        return None


# ---- words and events --------------------------------------------------------------
def norm_word(word: str) -> str:
    """A word's key across events: lower case, a slash's terms sorted, the
    "(N+ times)" kept -- "Gas / Gasoline / Oil" == "oil / gas / gasoline"."""
    w = re.sub(r"\s+", " ", (word or "").strip().lower()).replace("’", "'")
    m = re.match(r"^(.*?)\s*(\(\d+\+ times?\))?$", w)
    terms = sorted({t.strip() for t in m.group(1).split("/") if t.strip()})
    return " / ".join(terms) + (" " + m.group(2) if m.group(2) else "")


def market_word(m: dict) -> str:
    return str((m.get("custom_strike") or {}).get("Word") or m.get("yes_sub_title") or "")


def event_type(text: str) -> str:
    """rally / telerally / interview / presser / cabinet / bilateral /
    address / roundtable, else remarks -- the first rule that matches."""
    for t, rx in _TYPE_RES:
        if rx.search(text or ""):
            return t
    return "remarks"


def market_event_text(m: dict) -> str:
    """The market's title and the event its rules name ("... as part of
    Midterm Rally in Clemson, South Carolina originally scheduled for
    October 21, 2026, then ...") -- the same text in history and live."""
    rp = re.sub(r"\s+", " ", str(m.get("rules_primary") or ""))
    hit = _RULES_EVENT_RE.search(rp)
    return f"{m.get('title') or ''} || {hit.group(1) if hit else rp}"


def event_ts(event: str, m: Optional[dict] = None) -> float:
    """When the event ran, for the recency weights: the ticker's day at
    16:00Z (noon ET) or the market's close if earlier (a few old tickers
    carry a later day)."""
    out = None
    hit = _TICKER_DAY_RE.search(event or "")
    if hit:
        try:
            out = datetime(2000 + int(hit.group(1)), _MONTHS[hit.group(2)], int(hit.group(3)),
                           16, tzinfo=timezone.utc).timestamp()
        except ValueError:
            out = None
    close = _epoch((m or {}).get("close_time")) if m else None
    if close is not None and (out is None or close < out):
        out = close
    return float(out if out is not None else time.time())


# ---- Kalshi ------------------------------------------------------------------------
def _http_json(path: str, params: dict, get_text: Optional[Callable] = None) -> dict:
    """The public endpoint: injected get_text(url, params) -> JSON text, else
    requests, spaced REQUEST_GAP_SECS apart and retried on 429 / 5xx."""
    url = KALSHI_API + path
    if get_text is not None:
        js = json.loads(get_text(url, dict(params)))
        return js if isinstance(js, dict) else {}
    for attempt in range(len(RETRY_SLEEPS) + 1):
        wait = REQUEST_GAP_SECS - (time.time() - _last_request[0])
        if wait > 0:
            time.sleep(wait)
        _last_request[0] = time.time()
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        if (r.status_code == 429 or r.status_code >= 500) and attempt < len(RETRY_SLEEPS):
            time.sleep(RETRY_SLEEPS[attempt])
            continue
        r.raise_for_status()
        js = r.json()
        return js if isinstance(js, dict) else {}
    return {}


def _get(path: str, params: dict, get_json: Optional[Callable] = None,
         get_text: Optional[Callable] = None) -> dict:
    """The bot's signed reader first (get_json(path, params) -> dict), the
    public endpoint when it is absent or fails."""
    if get_json is not None:
        try:
            js = get_json(path, dict(params))
            if isinstance(js, dict):
                return js
        except Exception:
            pass
    return _http_json(path, params, get_text)


def _paged(path: str, params: dict, key: str, get_json: Optional[Callable],
           get_text: Optional[Callable]) -> List[dict]:
    out: List[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        js = _get(path, p, get_json, get_text)
        out.extend(x for x in (js.get(key) or []) if isinstance(x, dict))
        cursor = js.get("cursor")
        if not cursor:
            return out
    raise RuntimeError(f"{path} ran past {MAX_PAGES} pages")


def fetch_settled(since: Optional[float], get_json: Optional[Callable] = None,
                  get_text: Optional[Callable] = None) -> List[dict]:
    """Both series' markets settled since `since` (epoch s); None = all of
    them, the pre-cutoff archive (/historical/markets) included."""
    out: List[dict] = []
    for s in SERIES:
        params = {"series_ticker": s, "status": "settled", "limit": 1000}
        if since is not None:
            params["min_settled_ts"] = int(since)
        out += _paged("/markets", params, "markets", get_json, get_text)
        if since is None:
            out += _paged("/historical/markets", {"series_ticker": s, "limit": 1000},
                          "markets", get_json, get_text)
    return out


def fetch_open(get_json: Optional[Callable] = None,
               get_text: Optional[Callable] = None) -> Dict[str, dict]:
    """Open events: event -> {"title", "type", "live" (a market already closed
    or settled: under way or over), "markets": {ticker: {"word", "key",
    "bid", "ask"}}} (bid / ask in cents, for the dry run's display)."""
    out: Dict[str, dict] = {}
    for s in SERIES:
        evs = _paged("/events", {"series_ticker": s, "status": "open",
                                 "with_nested_markets": "true", "limit": 200},
                     "events", get_json, get_text)
        for e in evs:
            ev = str(e.get("event_ticker") or "")
            ms = [m for m in (e.get("markets") or []) if isinstance(m, dict) and m.get("ticker")]
            if not ev or not ms:
                continue
            live = any(str(m.get("status") or "") in _DONE for m in ms)
            markets = {}
            for m in ms:
                w = market_word(m)
                if str(m.get("status") or "") in _TRADING and w:
                    markets[str(m["ticker"])] = {
                        "word": w, "key": norm_word(w),
                        "bid": _cents(m.get("yes_bid_dollars")),
                        "ask": _cents(m.get("yes_ask_dollars"))}
            out[ev] = {"title": str(e.get("title") or ""), "sub_title": str(e.get("sub_title") or ""),
                       "type": event_type(market_event_text(ms[0])), "live": live,
                       "markets": markets}
    return out


# ---- the history -------------------------------------------------------------------
def absorb(history: Dict[str, dict], markets: List[dict]) -> Tuple[int, float]:
    """Fold settled markets into history {event: {"t", "type", "w": {key: 0/1}}}.
    Only a YES / NO result counts (voided and scalar events stay out).
    Returns (words added or changed, the newest settlement seen)."""
    added, newest = 0, 0.0
    for m in markets:
        res = str(m.get("result") or "").lower()
        ev = str(m.get("event_ticker") or "")
        key = norm_word(market_word(m))
        if res not in ("yes", "no") or not ev or not key:
            continue
        y = 1 if res == "yes" else 0
        e = history.get(ev)
        if e is None:
            e = history[ev] = {"t": int(event_ts(ev, m)),
                               "type": event_type(market_event_text(m)), "w": {}}
        else:
            e["t"] = int(min(e["t"], event_ts(ev, m)))
        if e["w"].get(key) != y:
            e["w"][key] = y
            added += 1
        st = _epoch(m.get("settlement_ts")) or _epoch(m.get("close_time")) or 0.0
        newest = max(newest, st)
    return added, newest


def is_dud(words: Dict[str, int]) -> bool:
    """The event never qualified: its "Event does not qualify" settled YES,
    or (without one) 10+ words all settled NO."""
    if EDQ_KEY in words:
        return words[EDQ_KEY] == 1
    rest = [y for k, y in words.items() if k != EDQ_KEY]
    return len(rest) >= 10 and not any(rest)


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _expit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def fit(history: Dict[str, dict], now: float) -> dict:
    """The recency-weighted counts (see the module docstring)."""
    NT: Dict[str, float] = {}
    YT: Dict[str, float] = {}
    DW: Dict[str, float] = {}
    DY: Dict[str, float] = {}
    tot_w = tot_y = 0.0
    rows = []
    for e in history.values():
        age = max(0.0, (now - float(e.get("t") or now)) / 86400.0)
        w1 = 0.5 ** (age / HALF_LIFE_WORD_TYPE_DAYS)
        w2 = 0.5 ** (age / HALF_LIFE_DAYS)
        typ = str(e.get("type") or "remarks")
        words = e.get("w") or {}
        DW[typ] = DW.get(typ, 0.0) + w2
        DY[typ] = DY.get(typ, 0.0) + w2 * is_dud(words)
        for k, y in words.items():
            if k == EDQ_KEY:
                continue
            NT[typ] = NT.get(typ, 0.0) + w2
            YT[typ] = YT.get(typ, 0.0) + w2 * y
            tot_w += w2
            tot_y += w2 * y
            rows.append((k, typ, y, w1, w2))
    p_all = (tot_y + 1.0) / (tot_w + 2.0)
    base = {t: (YT[t] + TYPE_PRIOR_N * p_all) / (NT[t] + TYPE_PRIOR_N) for t in NT}
    Nk: Dict[str, float] = {}
    Yk: Dict[str, float] = {}
    Ek: Dict[str, float] = {}
    NkT: Dict[Tuple[str, str], float] = {}
    YkT: Dict[Tuple[str, str], float] = {}
    for k, typ, y, w1, w2 in rows:
        Nk[k] = Nk.get(k, 0.0) + w2
        Yk[k] = Yk.get(k, 0.0) + w2 * y
        Ek[k] = Ek.get(k, 0.0) + w2 * base[typ]
        NkT[(k, typ)] = NkT.get((k, typ), 0.0) + w1
        YkT[(k, typ)] = YkT.get((k, typ), 0.0) + w1 * y
    dud_all = (sum(DY.values()) + 1.0) / (sum(DW.values()) + 20.0)
    dud = {t: (DY[t] + DUD_PRIOR_N * dud_all) / (DW[t] + DUD_PRIOR_N) for t in DW}
    return {"p_all": p_all, "base": base, "Nk": Nk, "Yk": Yk, "Ek": Ek, "NkT": NkT,
            "YkT": YkT, "dud_all": dud_all, "dud": dud, "events": len(history)}


def price(model: dict, key: str, typ: str) -> Tuple[float, dict]:
    """(p YES, diagnostics) for word `key` at an event of type `typ`."""
    if key == EDQ_KEY:
        p = model["dud"].get(typ, model["dud_all"])
        return min(max(p, P_MIN), P_MAX), {"n_eff": None, "n_type": None, "base": None}
    base = model["base"].get(typ, model["p_all"])
    nk = model["Nk"].get(key, 0.0)
    ebar = model["Ek"][key] / nk if nk > 0 else base
    lift = _logit((model["Yk"].get(key, 0.0) + WORD_PRIOR_N * ebar) / (nk + WORD_PRIOR_N)) \
        - _logit(ebar)
    prior = _expit(_logit(base) + lift)
    n = model["NkT"].get((key, typ), 0.0)
    p = (model["YkT"].get((key, typ), 0.0) + WORD_TYPE_PRIOR_N * prior) / (n + WORD_TYPE_PRIOR_N)
    return min(max(p, P_MIN), P_MAX), {"n_eff": round(nk, 2), "n_type": round(n, 2),
                                       "base": round(base, 4)}


# ---- the file ----------------------------------------------------------------------
def write_fair_file(path: str, now: Optional[datetime] = None,
                    get_json: Optional[Callable] = None,
                    get_text: Optional[Callable] = None) -> Tuple[int, int]:
    """Build and atomically write the fair file; returns (markets with a fair,
    open events without one). The settled history is read every
    HISTORY_TTL_SECS (incrementally; a failed read keeps the old history and
    its old read time, so the fairs age toward the gate's TTL), Kalshi's
    open events every EVENTS_TTL_SECS (a failed read reuses the cached list).
    An event under way or over, or with no history read ever, gets no fair."""
    now = now or datetime.now(timezone.utc)
    ts = now.timestamp()
    old = _read_json(path)
    history: Dict[str, dict] = dict(old.get("history") or {})
    through = float(old.get("history_through") or 0.0)
    hist_at = float(old.get("history_at") or 0.0)
    if not history:
        seed = _read_json(SEED_FILE)
        history = dict(seed.get("history") or {})
        through = float(seed.get("history_through") or 0.0)
        hist_at = float(seed.get("history_at") or 0.0)
    if not history or ts - hist_at >= HISTORY_TTL_SECS:
        cold = not history or through <= 0
        try:
            got = fetch_settled(None if cold else through - HISTORY_OVERLAP_SECS, get_json, get_text)
            added, newest = absorb(history, got)
            through = max(through, newest)
            hist_at = ts
            if added:
                _log(f"history: {added} settled word(s) added, {len(history)} events")
        except Exception as e:
            _log(f"! settled-history read failed ({type(e).__name__}: {str(e)[:100]}); keeping "
                 f"{len(history)} events read {_iso(hist_at) if hist_at else 'never'}")
    for ev in [ev for ev, e in history.items() if ts - float(e.get("t") or 0) > KEEP_DAYS * 86400]:
        history.pop(ev)
    ev_cache = old.get("open_events") or {}
    events_at = float(old.get("events_at") or 0.0)
    if ev_cache and ts - events_at < EVENTS_TTL_SECS:
        events = ev_cache
    else:
        try:
            events = fetch_open(get_json, get_text)
            events_at = ts
        except Exception as e:
            _log(f"! open-event read failed ({type(e).__name__}: {str(e)[:100]}); reusing "
                 f"{len(ev_cache)} cached")
            events = ev_cache
    model = fit(history, ts) if history and hist_at > 0 and events_at > 0 else None
    fetched = _iso(min(hist_at, events_at)) if model is not None else None
    markets: Dict[str, dict] = {}
    event_info: Dict[str, dict] = {}
    missing = 0
    for ev, e in sorted(events.items()):
        typ = e.get("type") or "remarks"
        info = {"type": typ, "title": e.get("title"), "priced": 0, "omitted": []}
        if e.get("live") or model is None:
            info["why"] = ("under way or over" if e.get("live") else
                           "no settled history read" if not (history and hist_at > 0) else
                           "no open-event read")
            event_info[ev] = info
            missing += 1
            continue
        for t, mk in sorted((e.get("markets") or {}).items()):
            p, diag = price(model, mk["key"], typ)
            if mk["key"] != EDQ_KEY and (diag["n_eff"] or 0.0) < MIN_HISTORY_N:
                info["omitted"].append(t)
                continue
            markets[t] = dict({"p": round(p, 4), "event": ev, "fetched_at": fetched,
                               "word": mk["word"], "type": typ}, **diag)
            info["priced"] += 1
        if not info["priced"]:
            missing += 1
        event_info[ev] = info
    out = {"generated_at": _iso(ts), "series": list(SERIES),
           "model": {"half_life_word_type_days": HALF_LIFE_WORD_TYPE_DAYS,
                     "half_life_days": HALF_LIFE_DAYS, "word_type_prior_n": WORD_TYPE_PRIOR_N,
                     "word_prior_n": WORD_PRIOR_N, "type_prior_n": TYPE_PRIOR_N,
                     "min_history_n": MIN_HISTORY_N,
                     "base": {t: round(v, 4) for t, v in sorted((model or {}).get("base", {}).items())},
                     "dud": {t: round(v, 4) for t, v in sorted((model or {}).get("dud", {}).items())}},
           "markets": markets, "event_fair": event_info,
           "open_events": events, "events_at": events_at,
           "history_at": hist_at, "history_through": through, "history": history}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, path)
    return len(markets), missing


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"the fair file to write (default {DEFAULT_OUT}; never the bot's)")
    ap.add_argument("--tol", type=float, default=RECOMMENDED_TOL_CENTS,
                    help="flag a touch this many cents through the fair")
    args = ap.parse_args(argv)
    ok, miss = write_fair_file(args.out)
    js = _read_json(args.out)
    print(f"history: {len(js.get('history') or {})} events, read "
          f"{_iso(js['history_at']) if js.get('history_at') else 'never'}; open events and "
          f"books read {_iso(js['events_at']) if js.get('events_at') else 'never'}")
    mk = js.get("markets") or {}
    for ev, info in sorted((js.get("event_fair") or {}).items()):
        e = (js.get("open_events") or {}).get(ev) or {}
        print(f"{ev} [{info.get('type')}] {info.get('title') or ''}"
              + (f" -- {info['why']}" if info.get("why") else ""))
        for t, m in sorted((e.get("markets") or {}).items()):
            f = mk.get(t)
            bid, ask = m.get("bid"), m.get("ask")
            flag = ""
            if f is not None and bid is not None and bid > 100 * f["p"] + args.tol:
                flag = "  <- bid capped"
            if f is not None and ask is not None and ask < 100 * f["p"] - args.tol:
                flag = "  <- ask capped"
            fair = f"{100 * f['p']:5.1f}c" if f is not None else "   --  "
            n = f" n_eff {f['n_eff']:6.2f}" if f is not None and f.get("n_eff") is not None else ""
            print(f"  {t:36s} {m['word'][:30]:30s} fair {fair}  book {bid}x{ask}{n}{flag}")
    print(f"{ok} markets priced, {miss} open events without a fair -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
