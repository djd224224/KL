#!/usr/bin/env python3
"""imm_unquoted.py -- the paying series the IMM does not quote, scored for
adding. Jack 2026-11-01: "yes" to a weekly email of the unquoted list, and
"also in the command center dashboard, make sure the 'promising' unquoted
markets section is built out and would include any promising ones".

One scorer, two readers: the weekly email (imm_unquoted_weekly.py) and the
dashboard's Opportunities section (imm_dashboard.fetch_unquoted), so both
show the same numbers and the same calls.

WHAT IS SCORED: the dashboard's gap rows (the 7:20 quote-gaps classifier,
cached as api_gaps.pkl) whose reason is "not in allowlist" -- paying series
outside every family and allow list -- rolled up per series. A series the
bot allows by the time it is scored (a hot-loaded enrollment file the gap
pass did not read) is dropped as enrolled.

THE ESTIMATE is the family watch's REAL ESTIMATES path: an offline, log-only
bot builds each paying market's meta (imm_quote_gaps.build_meta), screens it
as a fresh candidate (_screen: past its cutoff, one-sided, wide, a mid
outside 5-90c...), and runs _estimate_candidate_yield on the live book; a
market's figure is floor_dollars_per_day, the forward $/day over the bot's
size schedule, with no $1 floor. A screened market earns nothing. At x1
(the size extra_allow_series.json gives) and x3 (a code override's size,
the KXTRUMPVH wire: SERIES_OVERRIDES size_mult), each book read once,
through the public endpoint (kalshi_reads, prefer="public") so bulk book
reads spend nothing of the account's signed budget. A per-event top-N cap
keeps that many markets. The payout bar check counts the markets whose
total over the rest of their program clears floor_bar_dollars.

THE TAPE: makers' P&L to settlement on the series' public trades of the
last TAPE_DAYS -- every market that settled in the window (at most
TAPE_MAX_MARKETS, the busiest of each event first), its trades before the
bot's own cutoff for it (incentive_mm.trade_cutoff_utc; the close where it
has none), in cents per contract: priced 5-95c (where the bot rests
quotes) and 25-75c. Negative = makers were picked off. Cached per series
for TAPE_TTL_DAYS beside the series' Kalshi record.

THE CALL (first rule that applies; Jack's rules -- "scheduled or slow public
data", "one-off news is ok if it's not easy to be adversely selected
against" -- and the family watch's quote_call words):
  DON'T QUOTE    blocklisted or kept out on purpose, or another of Jack's
                 bots trades it (imm_family_watch.owner_of)
  PASS           no market can quote now (no two-sided book with a mid in
                 5-90c, or past its cutoff); no market clears the payout bar
                 even at x3; someone decides or announces it (insiders know
                 first); a live feed settles it (the bot's own open-scan rule,
                 incentive_mm.sources_are_live); makers lost TAPE_PASS_C or
                 more a contract; a one-off with no sign it moves slowly in
                 public; an event market (not scheduled data) with too
                 little settled tape to show makers hold up
  ADD WITH CARE  a mention; a vote; a one-off that moves through a slow
                 public process; scheduled data with too little tape; makers
                 lost TAPE_CARE_C or more; an event market whose tape held up
  ADD            scheduled or slow public data with a clean tape
Worth adding (promising) = ADD or ADD WITH CARE on PROMISING_MIN_X3 $/day or
more at x3. A call is advice: nothing here enrolls anything.

Read-only against the live system: the offline bot never runs a cycle,
places or cancels anything, or saves its state.
"""

import json
import os
import re
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

CANDIDATE_REASON = "not in allowlist"
MULTS = (1.0, 3.0)
CALLS = ("ADD", "ADD WITH CARE", "PASS", "DON'T QUOTE")
PROMISING_CALLS = frozenset(("ADD", "ADD WITH CARE"))
# worth adding = one of those calls and at least this much at x3 (Jack
# 2026-10-22: "if a market is only a few dollars a day, still tell me about
# it"; under $1/day even at x3 it is named, not proposed)
PROMISING_MIN_X3 = float(os.environ.get("IMM_UNQUOTED_PROMISING_MIN_X3", "1.0"))

MAX_BOOKS = int(os.environ.get("IMM_UNQUOTED_MAX_BOOKS", "1500"))
BOOK_PACE_SECS = float(os.environ.get("IMM_UNQUOTED_BOOK_PACE", "0.05"))

TAPE_DAYS = int(os.environ.get("IMM_UNQUOTED_TAPE_DAYS", "75"))
TAPE_TTL_DAYS = float(os.environ.get("IMM_UNQUOTED_TAPE_TTL_DAYS", "6.5"))
TAPE_MAX_MARKETS = int(os.environ.get("IMM_UNQUOTED_TAPE_MARKETS", "24"))
TAPE_MAX_PAGES = int(os.environ.get("IMM_UNQUOTED_TAPE_PAGES", "5"))
TAPE_BAND = (5, 95)
TAPE_MID = (25, 75)
TAPE_MIN_CTS = float(os.environ.get("IMM_UNQUOTED_TAPE_MIN_CTS", "2000"))
TAPE_MIN_EVENTS = int(os.environ.get("IMM_UNQUOTED_TAPE_MIN_EVENTS", "2"))
TAPE_CARE_C = float(os.environ.get("IMM_UNQUOTED_TAPE_CARE_C", "-2.0"))
TAPE_PASS_C = float(os.environ.get("IMM_UNQUOTED_TAPE_PASS_C", "-5.0"))
# below this x3 $/day a series is not proposed whatever its tape: not read
TAPE_MIN_X3 = PROMISING_MIN_X3
META_TTL_DAYS = 7.0
TAPE_CACHE_NAME = "unquoted_tape.json"

# the bot's screens that mean "cannot quote it now" (imm_quote_gaps.DEAD_REASONS)
_DEAD = frozenset(("cutoff", "no_event_window", "closing", "program_over", "no_target"))
_SCREEN_WORDS = {
    "cutoff": "past its cutoff", "closing": "closing within the hour",
    "program_over": "its program is over", "no_event_window": "no event window",
    "one_sided": "a one-sided book", "wide": "a spread too wide to join",
    "extreme_mid": "a mid outside 5-90c", "no_target": "no reward target",
}


def _f(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


def _ts(s) -> Optional[float]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ----------------------------------------------------------------------------
# The candidates
# ----------------------------------------------------------------------------

def candidates(gap_rows) -> Dict[str, dict]:
    """{series: {"series", "events", "n", "pool", "end", "title"}} from the
    gap rows marked CANDIDATE_REASON; events biggest pool first."""
    out: Dict[str, dict] = {}
    for r in gap_rows or []:
        if str(r.get("reason") or "") != CANDIDATE_REASON:
            continue
        s = str(r.get("series") or "")
        if not s:
            continue
        rec = out.setdefault(s, {"series": s, "events": [], "n": 0, "pool": 0.0,
                                 "end": None, "title": "", "_pools": {}})
        rec["_pools"][r["event"]] = _f(r.get("pool"))
        rec["n"] += int(r.get("n") or 0)
        rec["pool"] += _f(r.get("pool"))
        if r.get("end"):
            rec["end"] = max(rec["end"] or 0.0, _f(r["end"]))
        if not rec["title"] and r.get("title"):
            rec["title"] = str(r["title"])
    for rec in out.values():
        pools = rec.pop("_pools")
        rec["events"] = sorted(pools, key=lambda e: -pools[e])
        rec["pool"] = round(rec["pool"], 2)
    return out


# ----------------------------------------------------------------------------
# Kalshi reads (injectable for tests)
# ----------------------------------------------------------------------------

class Reads:
    """The Kalshi reads this module makes, through kalshi_reads: order books
    and trades public first (bulk single-market reads), series records and
    market lists signed first (the list reads Kalshi throttles unsigned)."""

    def __init__(self):
        from kalshi_reads import kalshi_get, kalshi_get_all
        self._get, self._get_all = kalshi_get, kalshi_get_all

    def book(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}/orderbook", prefer="public")

    def series(self, series: str) -> dict:
        return (self._get(f"/series/{series}") or {}).get("series") or {}

    def settled(self, series: str, since_ts: int) -> List[dict]:
        return self._get_all("/markets", {"series_ticker": series, "status": "settled",
                                          "min_close_ts": since_ts, "limit": 1000},
                             items_key="markets", max_pages=10)

    def trades(self, ticker: str, max_ts: Optional[int], max_pages: int) -> List[dict]:
        params = {"ticker": ticker, "limit": 1000}
        if max_ts:
            params["max_ts"] = int(max_ts)
        return self._get_all("/markets/trades", params, items_key="trades",
                             prefer="public", max_pages=max_pages, pace=0.1)


class PublicBooks:
    """The bot's client with its order-book reads sent to `get_book` (the
    public endpoint) and kept for the pass: x1 and x3 score the same book,
    read once. Everything else is the signed client's."""

    def __init__(self, client, get_book: Callable[[str], dict], pace: float = BOOK_PACE_SECS):
        self._client = client
        self._get_book = get_book
        self._pace = pace
        self.books: Dict[str, dict] = {}
        self.reads = 0

    def get_orderbook(self, ticker: str, depth=None):
        if ticker not in self.books:
            if self.reads and self._pace > 0:
                time.sleep(self._pace)
            self.reads += 1
            self.books[ticker] = self._get_book(ticker)
        return self.books[ticker]

    def __getattr__(self, name):
        return getattr(self._client, name)


def offline_bot(imm, client):
    """An offline bot for the estimator: never live, alerts log only (the
    family watch's _estimator)."""
    bot = imm.IncentiveMarketMaker(client, live=False)
    bot.alerter.enabled = False
    return bot


def reload_allowance(imm) -> None:
    """The enrollment files the bot hot-loads every refresh (refresh_universe),
    so "allowed" is what the live bot means now. The 7:20 classifier reads
    only extra_allow_series.json: on 11/01 the dashboard's gap list called the
    nine Carbon Arc arrays "not in allowlist" while finecon_extra_series.json
    held them."""
    for name in ("load_file_event_overrides", "load_extra_allow_series",
                 "load_finecon_extra_series", "load_election_extra_series",
                 "load_nfl_auto_series", "load_league_auto_series",
                 "load_awards_extra_series", "load_graduate_auto_series",
                 "load_mention_auto_series", "load_politics_auto_series",
                 "load_election_dates_extra",
                 "load_awards_dates_extra", "load_astest_picks"):
        fn = getattr(imm, name, None)
        if fn is not None:
            try:
                fn()
            except Exception as e:                  # one bad file must not cost the pass
                imm.log(f"[UNQUOTED] ! {name} failed: {e!r}")


# ----------------------------------------------------------------------------
# The estimate
# ----------------------------------------------------------------------------

@contextmanager
def size_at(imm, series: str, mult: float):
    """The series quoted at `mult` x its own size for the block: a temporary
    SERIES_OVERRIDES entry on the size_mult wire (ladder rungs, per-market and
    per-event caps scale together, as KXTRUMPVH's x3); a hand-tuned ladder
    (levels / max_position, which size_mult does not reach) is scaled
    instead. The series' own entry comes back after."""
    old = imm.SERIES_OVERRIDES.get(series)
    if mult == 1.0:
        yield
        return
    if old is not None and (old.levels or old.max_position is not None):
        new = replace(old, levels=[(t, max(1, int(round(s * mult))))
                                   for t, s in imm.series_levels(series)],
                      max_position=imm.series_max_position(series) * mult)
    else:
        base = (old.size_mult if old is not None and old.size_mult else 1.0)
        new = replace(old if old is not None else imm.SeriesOverride(), size_mult=base * mult)
    imm.SERIES_OVERRIDES[series] = new
    try:
        yield
    finally:
        if old is None:
            imm.SERIES_OVERRIDES.pop(series, None)
        else:
            imm.SERIES_OVERRIDES[series] = old


def _fresh(bot) -> None:
    """Forget per-market size modes an earlier estimate armed (the yield size
    mode): every estimate here is a fresh challenger's."""
    for name in ("_yield_boost", "_near_cliff_boost", "_rt_floor_boost"):
        d = getattr(bot, name, None)
        if isinstance(d, dict):
            d.clear()


def score_market(bot, imm, build_meta, t: str, info: dict, m: dict,
                 now: datetime, mults=MULTS, read_book: bool = True) -> dict:
    """One paying market as a fresh candidate: {"t", "ev", "screen" ("" =
    quotable), "days" (quotable days left), "est" {mult: $/day},
    "unreadable", "skipped"}. A screened market reads no book; with
    `read_book` false a quotable one is left unestimated ("skipped")."""
    meta = build_meta(bot, t, info, m, now)
    out = {"t": t, "ev": meta.event_ticker, "screen": "", "est": {},
           "days": float(imm._quotable_days(meta, now)), "cutoff": meta.cutoff}
    why = bot._screen(meta, now)
    if why:
        out["screen"] = str(why)
        return out
    if not read_book:
        out["skipped"] = True
        return out
    for mult in mults:
        _fresh(bot)
        with size_at(imm, meta.series, mult):
            try:
                ok = bot._estimate_candidate_yield(meta, [])
            except Exception as e:                  # one bad market must not cost the rest
                imm.log(f"[UNQUOTED] ! estimate {t} x{mult:g} failed: {e!r}")
                ok = False
        if not ok:
            out["unreadable"] = True
            out["est"] = {}
            break
        out["est"][mult] = float(meta.floor_dollars_per_day or 0.0)
    return out


def roll_up(markets: List[dict], top_n: int, bar: float, mults=MULTS) -> dict:
    """A series' figures from its scored markets: per mult the $/day, the $
    over the rest of the programs and how many markets clear the payout bar
    -- the top_n best of each event when the bot caps it (0 = uncapped)."""
    out = {"quotable": sum(1 for x in markets if not x["screen"] and not x.get("unreadable")),
           "screens": Counter(x["screen"] for x in markets if x["screen"]),
           "unreadable": sum(1 for x in markets if x.get("unreadable"))}
    for mult in mults:
        by_ev: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
        for x in markets:
            if mult in x["est"]:
                by_ev[x["ev"]].append((x["est"][mult], x["days"]))
        day = total = 0.0
        clear = 0
        for vals in by_ev.values():
            vals.sort(reverse=True)
            for v, d in (vals[:top_n] if top_n > 0 else vals):
                day += v
                total += v * d
                clear += int(v * d >= bar)
        k = f"x{mult:g}"
        out[k], out[k + "_total"], out[k + "_bar"] = round(day, 2), round(total, 2), clear
    return out


# ----------------------------------------------------------------------------
# The tape
# ----------------------------------------------------------------------------

def _settle_value(m: dict) -> Optional[float]:
    v = m.get("settlement_value_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    r = str(m.get("result") or "").lower()
    return 1.0 if r == "yes" else 0.0 if r == "no" else None


def _pick_markets(ms: List[dict], cap: int) -> List[dict]:
    """The busiest settled market of each event first (newest events first),
    then the next busiest of each, up to `cap`."""
    by_ev: Dict[str, List[dict]] = defaultdict(list)
    for m in ms:
        by_ev[str(m.get("event_ticker") or "")].append(m)
    order = sorted(by_ev, key=lambda e: max(str(m.get("close_time") or "") for m in by_ev[e]),
                   reverse=True)
    for e in order:
        by_ev[e].sort(key=lambda m: -_f(m.get("volume_fp") or m.get("volume")))
    out: List[dict] = []
    rank = 0
    while len(out) < cap and any(len(by_ev[e]) > rank for e in order):
        for e in order:
            if rank < len(by_ev[e]) and len(out) < cap:
                out.append(by_ev[e][rank])
        rank += 1
    return out


def tape_read(series: str, now: datetime, imm, reads, days: int = TAPE_DAYS,
              max_markets: int = TAPE_MAX_MARKETS, max_pages: int = TAPE_MAX_PAGES) -> dict:
    """Makers' P&L to settlement on the series' settled markets of the last
    `days` (see THE TAPE): {"days", "markets", "events", "cts", "pnl",
    "cts_mid", "pnl_mid", "of"} -- cts / pnl ($) over trades priced in
    TAPE_BAND, *_mid in TAPE_MID; "of" = settled markets in the window."""
    since = int((now - timedelta(days=days)).timestamp())
    ms = [m for m in reads.settled(series, since) or [] if _settle_value(m) is not None]
    out = {"days": days, "markets": 0, "events": 0, "cts": 0.0, "pnl": 0.0,
           "cts_mid": 0.0, "pnl_mid": 0.0, "of": len(ms)}
    events = set()
    for m in _pick_markets(ms, max_markets):
        s = _settle_value(m)
        cut = imm.trade_cutoff_utc(str(m.get("event_ticker") or ""),
                                   imm.parse_iso_utc(m.get("occurrence_datetime") or ""),
                                   imm.parse_iso_utc(m.get("expected_expiration_time") or ""))
        end = cut.timestamp() if cut is not None else _ts(m.get("close_time"))
        close = _ts(m.get("close_time"))
        if end is not None and close is not None:
            end = min(end, close)
        try:
            trades = reads.trades(m["ticker"], int(end) if end else None, max_pages)
        except Exception as e:
            imm.log(f"[UNQUOTED] ! trades {m.get('ticker')} unreadable: {e!r}")
            continue
        out["markets"] += 1
        n_in = 0
        for tr in trades or []:
            p = _f(tr.get("yes_price_dollars"), -1.0)
            if p < 0:
                p = _f(tr.get("yes_price"), -100.0) / 100.0
            n = _f(tr.get("count_fp") or tr.get("count"))
            ts = _ts(tr.get("created_time"))
            if n <= 0 or p < 0 or (end is not None and ts is not None and ts > end):
                continue
            pnl = ((p - s) if tr.get("taker_side") == "yes" else (s - p)) * n
            c = p * 100.0
            if TAPE_BAND[0] <= c <= TAPE_BAND[1]:
                out["cts"] += n
                out["pnl"] += pnl
                n_in += 1
            if TAPE_MID[0] <= c <= TAPE_MID[1]:
                out["cts_mid"] += n
                out["pnl_mid"] += pnl
        if n_in:
            events.add(str(m.get("event_ticker") or ""))
    out["events"] = len(events)
    for k in ("cts", "pnl", "cts_mid", "pnl_mid"):
        out[k] = round(out[k], 2)
    return out


def tape_cents(tp: Optional[dict], mid: bool = False) -> Optional[float]:
    """Makers' P&L in cents a contract, or None with no trades."""
    if not tp:
        return None
    cts = tp.get("cts_mid" if mid else "cts") or 0.0
    return 100.0 * tp.get("pnl_mid" if mid else "pnl", 0.0) / cts if cts > 0 else None


def tape_thin(tp: Optional[dict]) -> bool:
    return not tp or (tp.get("cts") or 0.0) < TAPE_MIN_CTS \
        or (tp.get("events") or 0) < TAPE_MIN_EVENTS


def _kct(n: float) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else f"{n:.0f}"


def tape_text(tp: Optional[dict]) -> str:
    """'makers -4.9c/ct on 18.2k ct (25-75c -3.1c), 6 events, 75 days'."""
    if tp is None:
        return "tape not read yet"
    c = tape_cents(tp)
    if c is None:
        return (f"no settled trades in {tp.get('days', TAPE_DAYS)} days"
                + (f" ({tp['of']} settled markets)" if tp.get("of") else ""))
    mid = tape_cents(tp, mid=True)
    return (f"makers {c:+.1f}c/ct on {_kct(tp['cts'])} ct"
            + (f" (25-75c {mid:+.1f}c)" if mid is not None else "")
            + f", {tp['events']} event{'s' if tp['events'] != 1 else ''}, "
              f"{tp.get('days', TAPE_DAYS)} days")


# ----------------------------------------------------------------------------
# The shared cache: Kalshi's series records and the tapes
# ----------------------------------------------------------------------------

def load_cache(path: Optional[str]) -> dict:
    if not path:
        return {"meta": {}, "tape": {}}
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f) or {}
    except (OSError, ValueError):
        c = {}
    c.setdefault("meta", {})
    c.setdefault("tape", {})
    return c


def save_cache(path: Optional[str], cache: dict) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(cache, f, separators=(",", ":"))
        os.replace(path + ".tmp", path)
    except OSError:
        pass


def live_source(imm, series_obj: dict) -> str:
    """The settlement source that makes the series live by the bot's own
    open-scan rule (incentive_mm.sources_are_live: a live price feed,
    scoreboard, weather feed or view counter), or ''."""
    blobs = imm.series_source_blobs(series_obj)
    if not imm.sources_are_live(blobs):
        return ""
    for x in series_obj.get("settlement_sources") or []:
        blob = (str((x or {}).get("name") or "") + " " + str((x or {}).get("url") or "")).lower()
        if any(k in blob for k in imm.SCAN_LIVE_SOURCE_KEYWORDS):
            return str((x or {}).get("name") or (x or {}).get("url") or "a live source")
    return "a live source"


def series_meta(series: str, cache: dict, reads, now_ts: float, imm=None) -> dict:
    """Kalshi's record for the series -- title, category, frequency, tags,
    settlement sources, the live one if any -- cached META_TTL_DAYS."""
    c = cache["meta"].get(series)
    if c and "live" in c and now_ts - _f(c.get("at")) < META_TTL_DAYS * 86400:
        return c
    try:
        s = reads.series(series) or {}
    except Exception:
        return c or {}
    c = {"at": now_ts, "title": str(s.get("title") or "").strip(),
         "category": str(s.get("category") or ""), "frequency": str(s.get("frequency") or ""),
         "tags": list(s.get("tags") or [])[:4],
         "sources": sorted({str((x or {}).get("name") or "")
                            for x in (s.get("settlement_sources") or [])
                            if isinstance(x, dict) and (x or {}).get("name")})[:3],
         "live": live_source(imm, s) if imm is not None and s else ""}
    cache["meta"][series] = c
    return c


# ----------------------------------------------------------------------------
# The call
# ----------------------------------------------------------------------------

def _watch_rules():
    """The family watch's owner and one-off rules (its quote_call's), read at
    call time: imm_family_watch imports the bot, so not at module import."""
    import imm_family_watch as fw
    return fw


RECURRING = frozenset(("daily", "weekly", "monthly", "quarterly"))
DATA_CATEGORIES = frozenset(("Economics", "Financials", "Climate and Weather"))
# where "win" / "elected" mean a vote (Who will win Dancing with the Stars?
# is a TV show, not a vote)
VOTE_CATEGORIES = frozenset(("Elections", "Politics", "World"))


def data_like(row: dict) -> bool:
    """Scheduled or slow public data, Jack's core: a recurring series, or an
    economic / financial / weather one that is not a one-off -- as against
    an event market (a season, a pick, a ruling...)."""
    freq = str(row.get("frequency") or "")
    return freq in RECURRING or (str(row.get("category") or "") in DATA_CATEGORIES
                                 and freq != "one_off")


def one_off_kind(title: str, frequency: str, category: str = "") -> Tuple[str, str]:
    """How the answer to an event series arrives, read off its title by the
    family watch's rules: ("insider", word) someone decides or announces it;
    ("vote", "") a vote or what a named vote decides; ("public", word) a
    slow public process; ("", "") neither. Custom and annual series take the
    insider test alone (most are recurring sports and stat markets); a
    recurring data series none."""
    fw = _watch_rules()
    if frequency in RECURRING:
        return "", ""
    if frequency != "one_off":
        w = fw.custom_insider_word(title)
        return ("insider", w) if w else ("", "")
    if category in VOTE_CATEGORIES and (fw._VOTE_RE.search(title) or fw._POST_VOTE_RE.search(title)) \
            and not fw._NOT_VOTE_RE.search(title):
        return "vote", ""
    m = fw._ONE_OFF_INSIDER_RE.search(title)
    if m:
        return "insider", m.group(0)
    m = fw._ONE_OFF_PUBLIC_RE.search(title)
    if m:
        return "public", m.group(0)
    return "", ""


def call_for(row: dict) -> Tuple[str, str]:
    """(call, one-line why) for a scored series (see THE CALL). Reads
    "owner", "quotable", "screens", "x3", "x3_bar", "bar", "title",
    "frequency", "category", "live", "tape" (None = not read)."""
    if row.get("owner"):
        return "DON'T QUOTE", (f"{row['owner']}: quoting it would cross that bot"
                               if row.get("owner_bot") else row["owner"])
    if not row.get("scored") and not row.get("unread"):
        return "PASS", "no paying market is open now (its program ended or its markets closed)"
    if not row.get("quotable"):
        top = Counter(row.get("screens") or {}).most_common(1)
        key = top[0][0] if top else ""
        if key in ("", "one_sided", "extreme_mid", "wide"):
            return "PASS", ("no two-sided book with a mid in 5-90c"
                            + (" (spreads too wide to join)" if key == "wide" else ""))
        return "PASS", f"no market can quote now: {_SCREEN_WORDS.get(key, key.replace('_', ' '))}"
    if not row.get("x3_bar"):
        return "PASS", (f"no market clears the ${row.get('bar', 1.3):.2f} payout bar even at x3 "
                        f"(~${row.get('x3', 0.0):,.2f}/day)")
    title = str(row.get("title") or "")
    freq = str(row.get("frequency") or "")
    cat = str(row.get("category") or "")
    kind, word = one_off_kind(title, freq, cat)
    if kind == "insider":
        return "PASS", f"someone decides or announces it (\"{word}\"): insiders know first"
    tp = row.get("tape")
    c = tape_cents(tp)
    if c is not None and (tp.get("cts") or 0.0) >= TAPE_MIN_CTS and c <= TAPE_PASS_C:
        return "PASS", f"adversely selected: {tape_text(tp)}"
    if row.get("live"):
        return "PASS", (f"a live feed settles it ({row['live']}): whoever watches it prices "
                        f"the market before us")
    if cat == "Mentions" or "MENTION" in str(row.get("series") or ""):
        return "ADD WITH CARE", "a mention market: quote it under the mention live gates"
    if kind == "vote":
        return "ADD WITH CARE", "a vote, or what a named vote decides: quote it once the vote's day is on file"
    if freq == "one_off" and kind != "public":
        return "PASS", "a one-off with no sign the answer comes out slowly in public"
    if tp is None:
        return "ADD WITH CARE", "its tape is not read yet"
    data = data_like(row)
    if c is None or tape_thin(tp):
        if data:
            return "ADD WITH CARE", f"public data, but too little settled tape to judge ({tape_text(tp)})"
        if kind == "public":
            return "ADD WITH CARE", (f"a one-off that moves through a slow public process "
                                     f"(\"{word}\"); {tape_text(tp)}")
        return "PASS", f"an event market with too little settled tape to show makers hold up ({tape_text(tp)})"
    if c <= TAPE_CARE_C:
        return "ADD WITH CARE", f"makers lose a little: {tape_text(tp)}"
    if kind == "public":
        return "ADD WITH CARE", f"a one-off that moves through a slow public process (\"{word}\"); {tape_text(tp)}"
    if data:
        return "ADD", (f"{freq} public data, " if freq in RECURRING else "public data, ") + tape_text(tp)
    return "ADD WITH CARE", f"an event market, but makers held up: {tape_text(tp)}"


def promising(row: dict) -> bool:
    """Worth adding: an ADD / ADD WITH CARE call on PROMISING_MIN_X3 or more
    at x3 -- what the weekly email proposes and the dashboard lists."""
    return row.get("call") in PROMISING_CALLS and row.get("x3", 0.0) >= PROMISING_MIN_X3


def needs_for(row: dict) -> str:
    """What adding the series would take, in words."""
    parts = []
    bar = row.get("bar", 1.3)
    if row.get("x1_bar"):
        parts.append("x1: add it to extra_allow_series.json (hot-loaded)")
    elif row.get("x3_bar"):
        parts.append(f"x3 only (no market clears the ${bar:.2f} bar at x1): a code override "
                     f"(size_mult, as KXTRUMPVH)")
    if row.get("x1_bar") and row.get("x3", 0.0) > row.get("x1", 0.0) + 0.5:
        parts.append("x3: a code override (size_mult, as KXTRUMPVH)")
    cat = str(row.get("category") or "")
    if cat == "Elections":
        parts.append("its election day row (imm_rows.py, two sources)")
    elif cat == "Mentions" or "MENTION" in str(row.get("series") or ""):
        parts.append("the mention live gates; a single event needs its start (--set)")
    elif row.get("undated"):
        parts.append("a stop time: its tickers carry no date, so it would quote to Kalshi's "
                     "close (--set)")
    return "; ".join(parts)


# ----------------------------------------------------------------------------
# What the family watch already sent
# ----------------------------------------------------------------------------

_ALERT_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}:\d{2}Z \[WATCH\] alert:")
_STAMPED = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z ")
_OLD_NEW = re.compile(r"\s([A-Z][A-Z0-9]+): newly paying \(")
# an item's first line: a series, or an event / market ticker (gap:, start:)
_ITEM_HEAD = re.compile(r"^([A-Z][A-Z0-9]*(?:-[A-Z0-9.]+)*): ")
_WORTH = re.compile(r"~\$([\d,]+(?:\.\d+)?)/day")


def watch_alerts(log_path: str) -> Dict[str, dict]:
    """{series: {"at": "YYYY-MM-DD", "est": $/day or None, "call": str}}: the
    new-series items the family watch emailed, from its log (the state file
    keeps no record: an alerted series just joins the baseline). Each of its
    formats: "- ... KXFOO: newly paying ($N/day pool) ..." (10/08-10/21), and
    "KXFOO: title" with a "Now: ...; needs a place on the allowlist" line
    (since 10/22). The first alert wins: the watch sends a series once."""
    out: Dict[str, dict] = {}
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    day, item = None, None

    def _close():
        if item and item.get("new") and "-" not in item["series"] \
                and item["series"] not in out:
            out[item["series"]] = {"at": item["at"], "est": item.get("est"),
                                   "call": item.get("call", "")}

    for ln in lines:
        m = _ALERT_HEAD.match(ln)
        if m:
            _close()
            day, item = m.group(1), None
            continue
        if day is None:
            continue
        if _STAMPED.match(ln):                      # the block is over
            _close()
            day, item = None, None
            continue
        if not ln.strip():                          # items are blank-line separated
            _close()
            item = None
            continue
        m = _OLD_NEW.search(ln) if ln.startswith("- ") else None
        if m:
            _close()
            w = _WORTH.search(ln)
            item = {"series": m.group(1), "at": day, "new": True,
                    "est": _f(w.group(1).replace(",", "")) if w else None}
            continue
        m = _ITEM_HEAD.match(ln)
        if m:
            _close()
            item = {"series": m.group(1), "at": day, "new": False}
            continue
        if item is None:
            continue
        s = ln.strip()
        if s.startswith("Now:") and "a place on the allowlist" in s:
            item["new"] = True
        elif s.startswith("Worth:"):
            w = _WORTH.search(s)
            item["est"] = _f(w.group(1).replace(",", "")) if w else None
        elif s.startswith("Call:"):
            item["call"] = s[len("Call:"):].strip().split(":", 1)[0].strip()
    _close()
    return out


def watch_status(series: str, state: dict, alerts: Dict[str, dict], x1: float) -> str:
    """What the 30-min family watch has done about the series, in words."""
    a = alerts.get(series)
    if a:
        when = datetime.strptime(a["at"], "%Y-%m-%d").strftime("%m/%d")
        txt = f"watch emailed it {when}"
        if a.get("est") is not None:
            txt += f" at ~${a['est']:,.2f}/day"
            if x1 >= 2.0 * max(a["est"], 0.5):
                txt += f"; it has grown to ~${x1:,.2f}"
        return txt + (f" ({a['call']})" if a.get("call") else "")
    it = (state.get("items") or {}).get(f"new:{series}")
    if it:
        return f"watch tracks it under its $5/day alert bar (~${_f(it.get('est')):,.2f}/day)"
    if series in set(state.get("baseline_not_allowed") or []):
        since = str(state.get("baseline_at") or "")[:10]
        return "never emailed: it was paying when the watch began" + (
            f" ({since[5:7]}/{since[8:10]})" if since else "")
    return "not emailed by the watch"


# ----------------------------------------------------------------------------
# The pass
# ----------------------------------------------------------------------------

def score_all(gap_rows, now: Optional[datetime] = None, imm=None, gaps_mod=None,
              bot=None, reads=None, max_books: Optional[int] = None,
              cache_path: Optional[str] = None, tape_budget: Optional[int] = None,
              watch_state_path: Optional[str] = None, watch_log_path: Optional[str] = None,
              build_meta=None) -> dict:
    """Score every CANDIDATE_REASON series of the gap rows. Returns {"rows":
    [one dict per series, by x1 $/day], "enrolled": [series the bot allows
    now], "ctx": {...}}. `max_books` bounds the book reads (biggest pools
    first; a market not reached leaves its series "partial"), `tape_budget`
    the series whose tape may be read this pass (None = all that need it);
    the cache at `cache_path` keeps series records and tapes between passes.
    The watch paths default to the bot's STATUS_DIR files."""
    t0 = time.time()
    now = now or datetime.now(timezone.utc)
    now_ts = now.timestamp()
    if imm is None or gaps_mod is None:
        import imm_quote_gaps as gaps_mod   # noqa: F811  (launcher env, then incentive_mm)
        import incentive_mm as imm          # noqa: F811
    build_meta = build_meta or gaps_mod.build_meta
    max_books = MAX_BOOKS if max_books is None else max_books
    reads = reads or Reads()
    rows = candidates(gap_rows)
    reload_allowance(imm)
    if bot is None:
        bot = offline_bot(imm, PublicBooks(imm.build_client(), reads.book))
    programs = bot.fetch_programs() or {}
    by_ev: Dict[str, List[str]] = defaultdict(list)
    for t, info in programs.items():
        if _f(info.get("dollars_per_day")) > 0:
            by_ev[t.rsplit("-", 1)[0]].append(t)
    enrolled = []
    for s in list(rows):
        tks = sorted(t for ev in rows[s]["events"] for t in by_ev.get(ev, []))
        rows[s]["tickers"] = tks
        if tks and bot._allowed(tks[0]):
            enrolled.append(s)
            del rows[s]
    client = getattr(bot, "client", None)
    details = gaps_mod.bulk_market_details(client, [t for r in rows.values() for t in r["tickers"]]) \
        if rows else {}
    cache = load_cache(cache_path)
    fw = _watch_rules()
    # Kalshi's records and the owners first: a series whose call cannot turn
    # on its estimate (owned, live, insiders know first) reads its books last
    for s, r in rows.items():
        meta = series_meta(s, cache, reads, now_ts, imm)
        r["title"] = meta.get("title") or r.get("title") or s
        r["category"], r["frequency"] = meta.get("category", ""), meta.get("frequency", "")
        r["sources"], r["live"] = meta.get("sources") or [], meta.get("live") or ""
        r["owner"], r["owner_bot"] = fw.owner_of(s)
        r["tape"] = None

    def _settled_without_books(s: str) -> bool:
        r = rows[s]
        return bool(r["owner"] or r["live"] or one_off_kind(
            r["title"], r["frequency"], r["category"])[0] == "insider")

    # estimates: the biggest pools first, each series' markets biggest first
    reads_before = getattr(client, "reads", 0)
    books = 0
    for s in sorted(rows, key=lambda s: (_settled_without_books(s), -rows[s]["pool"])):
        r = rows[s]
        imm.ensure_family_override(s)
        markets, unread = [], 0
        for t in sorted(r["tickers"], key=lambda t: -_f(programs[t].get("dollars_per_day"))):
            m = details.get(t)
            if not m or m.get("status") not in ("active", "open"):
                continue
            try:
                x = score_market(bot, imm, build_meta, t, programs[t], m, now,
                                 read_book=books < max_books)
            except Exception as e:
                imm.log(f"[UNQUOTED] ! {t} not scored: {e!r}")
                continue
            if x.get("skipped"):
                unread += 1
            elif not x["screen"]:
                books += 1
            markets.append(x)
        bar = float(imm.floor_bar_dollars(s, False))
        r.update(roll_up(markets, int(imm.event_top_n_for(s) or 0), bar))
        r["bar"], r["unread"] = bar, unread
        r["scored"] = len(markets)
        r["undated"] = bool(markets) and all(x.get("cutoff") is None for x in markets)
    # the calls that can turn on the tape
    need = []
    for s, r in rows.items():
        pre = call_for(r)                         # the call with no tape read
        tp = cache["tape"].get(s)
        if tp and now_ts - _f(tp.get("at")) < TAPE_TTL_DAYS * 86400:
            r["tape"] = tp
        elif pre[0] in PROMISING_CALLS and r.get("x3", 0.0) >= TAPE_MIN_X3:
            need.append(s)
        if r["tape"] is None and tp:              # a stale tape still beats none
            r["tape"], r["tape_stale"] = tp, True
    need.sort(key=lambda s: -rows[s].get("x3", 0.0))
    tape_reads = 0
    for s in need[:len(need) if tape_budget is None else max(0, tape_budget)]:
        t1 = time.time()
        try:
            tp = tape_read(s, now, imm, reads)
        except Exception as e:
            imm.log(f"[UNQUOTED] ! tape {s} unreadable: {e!r}")
            continue
        imm.log(f"[UNQUOTED] tape {s}: {tape_text(tp)} ({tp['markets']} markets, "
                f"{time.time() - t1:.0f}s)")
        tp["at"] = now_ts
        cache["tape"][s] = tp
        rows[s]["tape"], rows[s]["tape_stale"] = tp, False
        tape_reads += 1
    save_cache(cache_path, cache)
    # what the family watch has already sent
    status_dir = getattr(imm, "STATUS_DIR", "")
    try:
        with open(watch_state_path or os.path.join(status_dir, "family_watch_state.json"),
                  encoding="utf-8") as f:
            wstate = json.load(f) or {}
    except (OSError, ValueError):
        wstate = {}
    alerts = watch_alerts(watch_log_path or os.path.join(status_dir, "family-watch.log"))
    out = []
    for s, r in rows.items():
        r["call"], r["why"] = call_for(r)
        r["needs"] = needs_for(r) if r["call"] in PROMISING_CALLS else ""
        r["watch"] = watch_status(s, wstate, alerts, r.get("x1", 0.0))
        r["watch_alerted"] = s in alerts
        r["partial"] = bool(r.get("unread"))
        r["reason"] = CANDIDATE_REASON
        r["screens"] = dict(r.get("screens") or {})
        r.pop("tickers", None)
        out.append(r)
    out.sort(key=lambda r: (-r.get("x1", 0.0), -r.get("x3", 0.0), -r["pool"]))
    ctx = {"series": len(out), "pool": round(sum(r["pool"] for r in out), 2),
           "books": books, "book_reads": getattr(client, "reads", 0) - reads_before,
           "tape_reads": tape_reads, "tape_pending": max(0, len(need) - tape_reads),
           "enrolled": len(enrolled), "secs": round(time.time() - t0, 1),
           "calls": dict(Counter(r["call"] for r in out))}
    imm.log(f"[UNQUOTED] scored {len(out)} series ({books} books, {tape_reads} tapes, "
            f"{len(enrolled)} enrolled since the gap list) in {ctx['secs']}s")
    return {"rows": out, "enrolled": sorted(enrolled), "ctx": ctx}
