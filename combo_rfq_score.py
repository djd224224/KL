#!/usr/bin/env python3
r"""combo_rfq_score.py -- scores the combo maker SHADOW from combo_rfq_logger's
files (Jack 2026-11-05: "Yes, build it", on "Build a read-only combo RFQ
shadow logger and scorer first?").

THE QUESTION: would a simple combo maker -- quoting every eligible combo at
the independence fair of its legs plus a markup -- win flow from the makers
quoting today, and would the flow it wins pay?

Each logged execution (a combo print matched to an RFQ in the logger's 2%
slice; at most MAX_EXECS_PER_DAY a UTC day, picked by trade id) is scored:

  fair      the product of the legs' probabilities: a leg's YES mid
            (bid + ask) / 2 from Kalshi's 1-minute candles -- the last minute
            that ENDED before the RFQ was created, so no look-ahead -- and
            1 - mid for a NO leg
  eligible  every leg on a two-sided book no wider than MAX_LEG_SPREAD; no
            two legs on one game (the event ticker after its series:
            KXNBAPTS-26NOV05MIAGSW and KXNBAGAME-26NOV05MIAGSW are one game,
            as are the crypto 15-minute markets of one quarter-hour); every
            game still pre-game (start = the earliest expected expiration
            among its markets - GAME_HOURS: Kalshi sets game markets to
            expire ~3 h after the start, JAC@BAL 11/05 01:15Z -> 04:15Z)
  quote     at markup m: sell YES at fair x (1 + m), rounded UP to the
            combo's price grid; buy YES at fair x (1 - m), rounded down
  win       the taker bought YES at the print's price P and our ask < P, or
            sold YES at P and our bid > P: a strictly better price, which
            the requester would have taken. We fill the dollars the taker
            spent (target-cost RFQs, most of them) or the same contracts
            (contract-sized), at OUR price
  P&L       at the combo's settlement: short YES at a -> contracts x
            (a - payoff); long YES at b -> contracts x (payoff - b); less the
            maker fee MAKER_FEE_RATE x contracts x p x (1 - p). CONFIRMED
            11/05 (Jack: "confirm the maker fee on combos"): Kalshi's fee
            schedule (kalshi.com/fee-schedule) lists "Combos (excluding
            uncorrelated NFL)" with maker fees at 50% of taker, $0.04-$0.88
            per 100 contracts beside the taker's $0.07-$1.75 (= 0.07 x p(1-p)
            from 1c to 50c); the API docs define the series' fee_type
            quadratic_with_combo_maker_fees (since 2026-08-20) as a 0.5 maker
            multiplier, 0.25 on ordinary maker-fee series; a fill's fee
            rounds up to $0.000001 (docs fee_rounding). "Uncorrelated NFL" is
            not defined further, so every fill is charged and the report
            shows the part on all-NFL combos with no two legs on one game

Context on every scored execution: the makers' own P&L at the print price
(the flow's hold), the markup the market charges over the independence fair,
and the fair's calibration against outcomes. Combos on one night share
games, so their outcomes move together: with fewer than BOOT_MIN_DAYS days
the range comes from resampling anchor games (each combo's earliest game),
which understates the uncertainty.

Reads <dir>\combo_<UTC date>.jsonl[.gz]. Kalshi reads, authenticated with the
bots' key and throttled to ~2 a second: GET /markets?tickers= (the combos'
results, the legs' expirations; 100 a call) and GET /markets/candlesticks
(1-minute leg candles; 100 markets a call). Writes
combo_shadow_report_<ET date>.md and .json in <dir>. Places nothing.

  python combo_rfq_score.py [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--dir DIR]
"""
import argparse
import bisect
import collections
import gzip
import json
import math
import os
import random
import re
import sys
import time
import zlib
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from combo_rfq_logger import DEFAULT_OUT, SHARD_FACTOR  # noqa: E402
import combo_book_lines as book_lines  # noqa: E402  (the sportsbook's line: the second fair)

MARKUPS = (0.05, 0.10, 0.15, 0.20, 0.30, 0.50)
MAX_LEG_SPREAD = 0.10
GAME_HOURS = 3.0
MAKER_FEE_RATE = 0.035     # combo makers: 0.5 x the 0.07 taker rate (fee schedule, 11/05)
# STRATEGY 1, the live pilot's rule (combo_maker.py imports MAIN_SERIES; keep S1
# in step with its Config): US-league main lines only, 3-8 legs, legs <= 3c
# wide, fair 2-40c, every game >= 30 min from its start
US_MAIN = re.compile(r"^KX(NFL|NBA|WNBA|NHL|MLB|NCAAF|NCAAMB|NCAAWB)(GAME|SPREAD|TOTAL)$")
# MORE SPORTS (Jack 2026-11-05: "add all of those sports with 'expiry minus 3
# hours' rule"): the game-level markets of the leagues seen in that night's
# cross-game parlays. Soccer's start rule checked against ESPN (kickoff =
# expiry - 3.0 h: EPL 5/5, LaLiga 5/5, Bundesliga 3/3, Serie A 6/6, MLS 14/14,
# Eredivisie 7/7, ...); tennis, fights and the college first half are NOT
# checked (no schedule source), so the maker keeps an hour more margin there.
SOCCER_MAIN = re.compile(r"^KX(EPL|LALIGA|BUNDESLIGA|SERIEA|LIGUE1|MLS|LIGAMX|LIGAEXP|BRASILEIRO|BRASILEIROB|"
                         r"ARGPREMDIV|APFDDH|EREDIVISIE|UCL|UEL|UECL)(GAME|TOTAL|SPREAD)$")
OTHER_MAIN = re.compile(r"^KX(ITFDOUBLES|ITFMATCH|ITFWMATCH|ATPMATCH|WTAMATCH|ATPCHALLENGERMATCH|"
                        r"WTACHALLENGERMATCH|UFCFIGHT|BOXING)$")
# EXTRAS (Jack 2026-11-05: "Other deep game-level markets, such as first-half
# lines" and "Props on different games, only where both sides of the book have
# real depth ($1000+)"): US leagues only. Each needs $1,000+ at the best bid AND
# the best ask, and takes its start from its GAME's winner market (props can
# expire hours after the game: MIA@GSW props 09:00Z vs the game 06:00Z). Left
# out: the *PROT variants (settlement terms not checked) and first-scorer markets.
US_LEAGUE = r"(NFL|NBA|WNBA|NHL|MLB|NCAAF|NCAAMB|NCAAWB)"
EXTRA_GAME = re.compile(rf"^KX{US_LEAGUE}((1H|2H|1Q|2Q|3Q|4Q|1P)(SPREAD|TOTAL|TEAMTOTAL)?|TEAMTOTAL)$")
PROP = re.compile(rf"^KX{US_LEAGUE}(PTS|REB|AST|3PT|PRA|STL|BLK|2D|3D|RECYDS|RSHYDS|PASSYDS|REC|TD|"
                  r"PASSTDS|RSHATT|PASSINT|PASSCOMP|PASSATT|RRYDS|GOAL|SOG|SAVES|HITS|HR|KS|RBI)$")


def league_of(series: str):
    """'KXNBAPTS' -> 'NBA' (None outside the US leagues)."""
    m = re.match(rf"^KX{US_LEAGUE}", series)
    return m.group(1) if m else None


class _AnyMain:
    """MAIN_SERIES.match(series): a quotable game-level series of any group."""
    def match(self, s):
        return US_MAIN.match(s) or SOCCER_MAIN.match(s) or OTHER_MAIN.match(s)


MAIN_SERIES = _AnyMain()


def sport_group(series: str) -> str:
    """'us' / 'soccer' (start rule checked) or 'unchecked' (tennis, fights,
    the first half); '' when not quotable."""
    if US_MAIN.match(series):
        return "us"
    if SOCCER_MAIN.match(series):
        return "soccer"
    if OTHER_MAIN.match(series):
        return "unchecked"
    if EXTRA_GAME.match(series):
        return "extra"
    if PROP.match(series):
        return "prop"
    return ""
S1 = {"min_legs": 3, "max_legs": 8, "min_fair": 0.02, "max_fair": 0.40, "max_leg_spread": 0.03,
      "min_lead_min": 30.0, "markups": (0.05, 0.10, 0.15, 0.20)}
MAX_EXECS_PER_DAY = 5000
CALL_GAP_S = 0.5
BATCH = 100
CANDLE_PAD_S = 120
BOOT_N = 2000
BOOT_MIN_DAYS = 5
SEED = 20261105
LEG_BUCKETS = ((2, "2"), (3, "3"), (4, "4"), (5, "5"), (7, "6-7"), (10, "8-10"), (10 ** 9, "11+"))
FAIR_BUCKETS = ((0.01, "<1%"), (0.05, "1-5%"), (0.15, "5-15%"), (0.40, "15-40%"), (1.01, "40%+"))
# a combo's price grid when its market carries none (11/05: 0.0001 under 1c and
# over 99c, 0.001 between)
DEFAULT_GRID = [{"start": "0.0000", "end": "0.0100", "step": "0.0001"},
                {"start": "0.0100", "end": "0.9900", "step": "0.0010"},
                {"start": "0.9900", "end": "1.0000", "step": "0.0001"}]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} [COMBO-SCORE] {msg}", flush=True)


def et_zone():
    """US Eastern (the reports' date), with DST; pytz as the bots use it."""
    try:
        import pytz
        return pytz.timezone("US/Eastern")
    except ImportError:
        return timezone(timedelta(hours=-5))


def _f(x, dflt=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return dflt


def _iso_s(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def utc_day(ms) -> str:
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%d")


def leg_bucket(n: int) -> str:
    for top, lab in LEG_BUCKETS:
        if n <= top:
            return lab
    return LEG_BUCKETS[-1][1]


def fair_bucket(p: float) -> str:
    for top, lab in FAIR_BUCKETS:
        if p < top:
            return lab
    return FAIR_BUCKETS[-1][1]


def game_key(event_ticker: str) -> str:
    """The game an event belongs to: its ticker after the series
    (KXNBAPTS-26NOV05MIAGSW -> 26NOV05MIAGSW)."""
    e = str(event_ticker or "")
    return e.split("-", 1)[1] if "-" in e else e


def event_of_leg(ticker: str) -> str:
    """A leg's event from its own ticker when the market read lacks one: the
    series and its next segment (KXNBAPTS-26NOV05MIAGSW-GSWSCURRY30-25 ->
    KXNBAPTS-26NOV05MIAGSW), so a prop lands on its game, not on its player."""
    parts = str(ticker).split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else str(ticker)


# ---- the price grid -------------------------------------------------------------

def _step_at(p: float, grid) -> float:
    for r in grid or DEFAULT_GRID:
        lo, hi, st = _f(r.get("start"), 0.0), _f(r.get("end"), 1.0), _f(r.get("step"), 0.01)
        if lo - 1e-12 <= p < hi - 1e-12:
            return st
    return _f((grid or DEFAULT_GRID)[-1].get("step"), 0.01)


def grid_up(p: float, grid=None) -> float:
    """p rounded UP to the grid (a price we sell at)."""
    st = _step_at(p, grid)
    return round(math.ceil(p / st - 1e-9) * st, 6)


def grid_down(p: float, grid=None) -> float:
    """p rounded DOWN to the grid (a price we buy at)."""
    st = _step_at(p, grid)
    return round(math.floor(p / st + 1e-9) * st, 6)


def maker_fee(contracts: float, price: float, rate: float = MAKER_FEE_RATE) -> float:
    return rate * contracts * price * (1.0 - price)


# ---- the logs -------------------------------------------------------------------

def day_range(since: date, until: date):
    d = since
    while d <= until:
        yield d.isoformat()
        d += timedelta(days=1)


def load(dir_: str, since: date, until: date):
    """(executions deduped by trade id, sampled RFQs, stat lines, run lines)
    from the logger's day files, plain or gzip'd. A torn last line is skipped."""
    execs, rfqs, stats, runs = {}, [], [], []
    for day in day_range(since, until):
        for name in (f"combo_{day}.jsonl", f"combo_{day}.jsonl.gz"):
            path = os.path.join(dir_, name)
            if not os.path.exists(path):
                continue
            opener = gzip.open if name.endswith(".gz") else open
            try:
                with opener(path, "rt", encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue
                        k = r.get("k")
                        if k == "exec":
                            execs.setdefault(r.get("tid") or f"{r.get('mt')}@{r.get('ts_ms')}", r)
                        elif k == "rfq":
                            rfqs.append(r)
                        elif k == "stat":
                            stats.append(r)
                        elif k in ("start", "stop", "conn", "disc", "err"):
                            runs.append(r)
            except (OSError, EOFError) as e:
                log(f"! {name}: read stopped early ({e!r})")
    return list(execs.values()), rfqs, stats, runs


def pick(execs: list, per_day: int = MAX_EXECS_PER_DAY) -> list:
    """At most per_day executions a UTC day (of the RFQ), chosen by crc32 of
    the trade id: deterministic, and unrelated to price or outcome."""
    by = collections.defaultdict(list)
    for e in execs:
        by[utc_day(e["rfq_ms"])].append(e)
    out = []
    for day in sorted(by):
        rows = sorted(by[day], key=lambda e: zlib.crc32(str(e.get("tid") or "").encode()))
        out += rows[:per_day]
    return out


# ---- Kalshi reads -------------------------------------------------------------

class Reader:
    """Throttled reads through the bots' REST client (client.get(path, params))."""

    def __init__(self, client, gap_s: float = CALL_GAP_S):
        self.client = client
        self.gap_s = gap_s
        self.calls = 0
        self._last = 0.0

    def _get(self, path, params):
        wait = self._last + self.gap_s - time.time()
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        self.calls += 1
        return self.client.get(path, params)

    def markets(self, tickers) -> dict:
        out = {}
        ts = sorted(set(tickers))
        for i in range(0, len(ts), BATCH):
            chunk = ts[i:i + BATCH]
            try:
                r = self._get("/markets", {"tickers": ",".join(chunk), "limit": 1000})
            except Exception as e:
                log(f"! markets read failed ({len(chunk)} tickers): {e!r}")
                continue
            for m in r.get("markets") or []:
                if m.get("ticker"):
                    out[m["ticker"]] = m
        return out

    def candles(self, tickers, start_s: int, end_s: int) -> dict:
        """{ticker: [(end_ts, bid, ask), ...] sorted}: 1-minute candles, plus
        the latest one before start_s."""
        out = {}
        ts = sorted(set(tickers))
        for i in range(0, len(ts), BATCH):
            chunk = ts[i:i + BATCH]
            try:
                r = self._get("/markets/candlesticks",
                              {"market_tickers": ",".join(chunk), "start_ts": int(start_s),
                               "end_ts": int(end_s), "period_interval": 1,
                               "include_latest_before_start": "true"})
            except Exception as e:
                log(f"! candles read failed ({len(chunk)} markets): {e!r}")
                continue
            for mk in r.get("markets") or []:
                rows = []
                for c in mk.get("candlesticks") or []:
                    end = c.get("end_period_ts")
                    bid = _f((c.get("yes_bid") or {}).get("close_dollars"))
                    ask = _f((c.get("yes_ask") or {}).get("close_dollars"))
                    if end is not None:
                        rows.append((int(end), bid, ask))
                out.setdefault(mk.get("market_ticker"), []).extend(rows)
        return out


class PriceBook:
    """Each leg's candles, merged across hour windows; price_at(t) = the last
    candle that ended at or before t."""

    def __init__(self):
        self.rows = collections.defaultdict(dict)        # ticker -> {end: (bid, ask)}
        self._sorted = {}

    def add(self, got: dict) -> None:
        for t, rows in got.items():
            for end, bid, ask in rows:
                self.rows[t][end] = (bid, ask)
            self._sorted.pop(t, None)

    def price_at(self, ticker: str, t_s: float):
        """(bid, ask, age_s) or None."""
        if ticker not in self.rows:
            return None
        ends = self._sorted.get(ticker)
        if ends is None:
            ends = self._sorted[ticker] = sorted(self.rows[ticker])
        i = bisect.bisect_right(ends, t_s) - 1
        if i < 0:
            return None
        bid, ask = self.rows[ticker][ends[i]]
        return bid, ask, t_s - ends[i]


def fetch(reader: Reader, execs: list):
    """(markets {ticker: market} for the combos and their legs, PriceBook)."""
    combos = {e["mt"] for e in execs}
    legs = {l[0] for e in execs for l in e["legs"]}
    log(f"reading {len(combos)} combos and {len(legs)} legs")
    mk = reader.markets(combos | legs)
    need = collections.defaultdict(set)                  # hour start -> legs
    for e in execs:
        h = int(e["rfq_ms"] // 1000 // 3600 * 3600)
        for l in e["legs"]:
            need[h].add(l[0])
    book = PriceBook()
    for n, h in enumerate(sorted(need)):
        book.add(reader.candles(need[h], h - CANDLE_PAD_S, h + 3600))
        if n % 6 == 5:
            log(f"candles: {n + 1} of {len(need)} hours, {reader.calls} calls")
    return mk, book


# ---- scoring ------------------------------------------------------------------

def payoff_of(m):
    """A settled combo's YES payoff (1, 0, or a scalar value), else None."""
    if not m:
        return None
    res = str(m.get("result") or "").lower()
    if res == "yes":
        return 1.0
    if res == "no":
        return 0.0
    v = _f(m.get("settlement_value_dollars"))
    if res == "scalar" or (v is not None and m.get("status") in ("determined", "finalized", "settled")):
        return v
    return None


def game_starts(markets: dict) -> dict:
    """game key -> estimated start (epoch s): the earliest expected
    expiration among its markets, less GAME_HOURS."""
    out = {}
    for t, m in markets.items():
        if str(t).startswith("KXMVE"):
            continue
        exp = _iso_s(m.get("expected_expiration_time"))
        if exp is None:
            continue
        k = game_key(m.get("event_ticker") or event_of_leg(t))
        out[k] = min(out.get(k, exp), exp)
    return {k: v - GAME_HOURS * 3600 for k, v in out.items()}


def score_exec(e: dict, markets: dict, book: PriceBook, starts: dict) -> dict:
    """One execution: its fair, why it is not eligible (None = eligible), the
    print's side / price / size, the payoff, and the makers' P&L at the print."""
    t = e["rfq_ms"] / 1000.0
    px, ct = _f(e.get("px"), 0.0), _f(e.get("ct"), 0.0)
    side = e.get("side") or "yes"
    combo = markets.get(e["mt"]) or {}
    keys, probs, widest, oldest, why = [], [], 0.0, 0.0, None
    for leg in e["legs"]:
        tk, sd = leg[0], leg[1]
        m = markets.get(tk) or {}
        keys.append(game_key(m.get("event_ticker") or event_of_leg(tk)))
        q = book.price_at(tk, t)
        if q is None or q[0] is None or q[1] is None or q[0] <= 0 or q[1] >= 1 or q[1] < q[0]:
            why = why or "no_price"
            continue
        bid, ask, age = q
        mid = (bid + ask) / 2.0
        probs.append(mid if sd == "yes" else 1.0 - mid)
        widest, oldest = max(widest, ask - bid), max(oldest, age)
    fair = math.prod(probs) if probs and why is None else None
    if why is None and widest > MAX_LEG_SPREAD + 1e-9:
        why = "wide"
    if why is None and len(set(keys)) < len(keys):
        why = "same_game"
    if why is None and any(k in starts and t >= starts[k] for k in keys):
        why = "in_play"
    if why is None and any(k not in starts for k in keys):
        why = "no_start"
    payoff = payoff_of(combo)
    prem = ct * (px if side == "yes" else 1.0 - px)
    mk_pnl = None if payoff is None else (ct * (px - payoff) if side == "yes" else ct * (payoff - px))
    anchor = min(keys, key=lambda k: starts.get(k, float("inf"))) if keys else ""
    in_play = any(k in starts and t >= starts[k] for k in keys)
    leads = [(starts[k] - t) / 60.0 for k in keys if k in starts]
    return {"tid": e.get("tid"), "day": utc_day(e["rfq_ms"]), "t": t, "mt": e["mt"],
            "n_legs": len(e["legs"]), "bucket": leg_bucket(len(e["legs"])), "side": side,
            "px": px, "ct": ct, "tc": e.get("tc") is not None, "prem": prem,
            "fair": fair, "why": why, "widest": widest, "oldest_s": oldest,
            "same_game": len(set(keys)) < len(keys), "in_play": in_play, "anchor": anchor,
            # the fee table's "uncorrelated NFL" exemption, read as: every leg
            # an NFL market and no two on one game (Kalshi defines it no further)
            "nfl_uncorr": bool(keys) and len(set(keys)) == len(keys)
            and all(str(l[0]).startswith("KXNFL") for l in e["legs"]),
            "main_only": all(MAIN_SERIES.match(str(l[0]).split("-")[0]) for l in e["legs"]),
            "lead_min": min(leads) if leads else None,
            "payoff": payoff, "mk_pnl": mk_pnl, "mk_fee": maker_fee(ct, px),
            "grid": combo.get("price_ranges") or DEFAULT_GRID}


def shadow_fill(s: dict, m: float):
    """Our fill on a scored execution at markup m, or None (not eligible, or
    our price not strictly better than the print). {cts, price, prem, fee,
    pnl (None while unsettled)}."""
    if s["why"] is not None or s["fair"] is None:
        return None
    f, px, ct = s["fair"], s["px"], s["ct"]
    if s["side"] == "yes":                       # the taker bought YES at px
        ask = grid_up(f * (1.0 + m), s["grid"])
        if not (0.0 < ask < 1.0) or not ask < px - 1e-12:
            return None
        cts = (ct * px / ask) if s["tc"] else ct
        pnl = None if s["payoff"] is None else cts * (ask - s["payoff"])
        return {"cts": cts, "price": ask, "prem": cts * ask, "fee": maker_fee(cts, ask), "pnl": pnl}
    bid = grid_down(f * (1.0 - m), s["grid"])    # the taker sold YES at px (bought NO)
    if not (0.0 < bid < 1.0) or not bid > px + 1e-12:
        return None
    cts = (ct * (1.0 - px) / (1.0 - bid)) if s["tc"] else ct
    pnl = None if s["payoff"] is None else cts * (s["payoff"] - bid)
    return {"cts": cts, "price": bid, "prem": cts * (1.0 - bid), "fee": maker_fee(cts, bid), "pnl": pnl}


def is_s1(s: dict) -> bool:
    """Strategy 1's rule on a scored execution: the pilot's filters, on the
    shadow's prices (the last 1-minute candle before the RFQ)."""
    return bool(s["why"] is None and s.get("main_only")
                and s["widest"] <= S1["max_leg_spread"] + 1e-9
                and S1["min_legs"] <= s["n_legs"] <= S1["max_legs"] and s["fair"] is not None
                and S1["min_fair"] <= s["fair"] <= S1["max_fair"]
                and (s.get("lead_min") or 0.0) >= S1["min_lead_min"])


def boot_ci(rows: list, value, cluster, n: int = BOOT_N, seed: int = SEED):
    """90% range of sum(value) by resampling clusters."""
    by = collections.defaultdict(float)
    for r in rows:
        by[cluster(r)] += value(r)
    vals = list(by.values())
    if len(vals) < 2:
        return None, None
    rng = random.Random(seed)
    sums = sorted(sum(vals[rng.randrange(len(vals))] for _ in vals) for _ in range(n))
    return sums[int(0.05 * n)], sums[int(0.95 * n) - 1]


def build(scored: list, markups=MARKUPS) -> dict:
    """Every table of the report, from the scored executions."""
    settled = [s for s in scored if s["payoff"] is not None]
    days = sorted({s["day"] for s in scored})
    by_day = len(days) >= BOOT_MIN_DAYS
    cluster = (lambda r: r["day"]) if by_day else (lambda r: r["anchor"])

    def flow(rows):
        prem = sum(r["prem"] for r in rows)
        gross = sum(r["mk_pnl"] for r in rows)
        fee = sum(r["mk_fee"] for r in rows)
        return {"n": len(rows), "prem": prem, "gross": gross, "fee": fee, "net": gross - fee,
                "hold": (gross / prem) if prem else None}

    out = {"days": days, "n_scored": len(scored), "n_settled": len(settled),
           "n_pending": len(scored) - len(settled), "ci_by": "day" if by_day else "anchor game",
           "why": dict(collections.Counter(s["why"] or "eligible" for s in scored)),
           "flow": flow(settled)}
    lo, hi = boot_ci(settled, lambda r: r["mk_pnl"], cluster)
    out["flow"]["ci"] = [lo, hi]
    out["flow_by"] = {}
    for name, f in (("taker bought YES", lambda s: s["side"] == "yes"),
                    ("taker sold YES", lambda s: s["side"] != "yes"),
                    ("eligible", lambda s: s["why"] is None),
                    ("same game", lambda s: s["same_game"]),
                    ("in play", lambda s: s["in_play"])):
        out["flow_by"][name] = flow([s for s in settled if f(s)])
    out["flow_legs"] = {lab: flow([s for s in settled if s["bucket"] == lab]) for _, lab in LEG_BUCKETS}

    elig = [s for s in scored if s["why"] is None]
    elig_set = [s for s in elig if s["payoff"] is not None]
    out["n_eligible"], out["n_eligible_settled"] = len(elig), len(elig_set)
    sh = []
    for m in markups:
        fills = [(s, shadow_fill(s, m)) for s in elig_set]
        won = [(s, f) for s, f in fills if f is not None]
        pend = sum(1 for s in elig if s["payoff"] is None and shadow_fill(s, m) is not None)
        gross = sum(f["pnl"] for _, f in won)
        fee = sum(f["fee"] for _, f in won)
        prem = sum(f["prem"] for _, f in won)
        rows = [{"day": s["day"], "anchor": s["anchor"], "v": f["pnl"] - f["fee"]} for s, f in won]
        lo, hi = boot_ci(rows, lambda r: r["v"], cluster) if rows else (None, None)
        sh.append({"markup": m, "eligible": len(elig_set), "wins": len(won),
                   "win_share": (len(won) / len(elig_set)) if elig_set else None,
                   "prem": prem, "gross": gross, "fee": fee, "net": gross - fee,
                   "fee_nfl": sum(f["fee"] for s, f in won if s.get("nfl_uncorr")),
                   "per_prem": ((gross - fee) / prem) if prem else None,
                   "ci": [lo, hi], "pending_wins": pend,
                   "by_legs": {lab: sum(f["pnl"] - f["fee"] for s, f in won if s["bucket"] == lab)
                               for _, lab in LEG_BUCKETS}})
    out["shadow"] = sh
    # Strategy 1, the live pilot's rule, on the same executions
    s1 = [s for s in elig if is_s1(s)]
    s1_set = [s for s in s1 if s["payoff"] is not None]
    out["s1"] = {"eligible": len(s1), "settled": len(s1_set),
                 "with_nfl": sum(1 for s in s1 if s.get("nfl_uncorr")), "rows": []}
    for m in S1["markups"]:
        won = [(s, f) for s in s1_set for f in [shadow_fill(s, m)] if f is not None]
        gross, fee = sum(f["pnl"] for _, f in won), sum(f["fee"] for _, f in won)
        prem = sum(f["prem"] for _, f in won)
        rows = [{"day": s["day"], "anchor": s["anchor"], "v": f["pnl"] - f["fee"]} for s, f in won]
        lo, hi = boot_ci(rows, lambda r: r["v"], cluster) if rows else (None, None)
        out["s1"]["rows"].append({
            "markup": m, "wins": len(won), "prem": prem, "gross": gross, "fee": fee,
            "net": gross - fee, "per_prem": ((gross - fee) / prem) if prem else None, "ci": [lo, hi],
            "pending_wins": sum(1 for s in s1 if s["payoff"] is None and shadow_fill(s, m) is not None)})
    # the market's markup over the independence fair (takers buying YES)
    mk = collections.defaultdict(list)
    for s in elig:
        if s["side"] == "yes" and s["fair"]:
            mk[s["bucket"]].append(s["px"] / s["fair"] - 1.0)
    out["markup"] = {lab: {"n": len(v), "p25": _q(v, 0.25), "median": _q(v, 0.5), "p75": _q(v, 0.75),
                           "below_fair": sum(1 for x in v if x < 0) / len(v)}
                     for lab, v in ((lab, mk.get(lab, [])) for _, lab in LEG_BUCKETS) if v}
    # calibration: the independence fair and the price paid vs what happened
    cal = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for s in elig_set:
        if s["side"] == "yes":
            c = cal[fair_bucket(s["fair"])]
            c[0] += 1
            c[1] += s["fair"]
            c[2] += s["px"]
            c[3] += s["payoff"]
    out["calibration"] = {}
    for _, lab in FAIR_BUCKETS:
        c = cal.get(lab)
        if c and c[0]:
            out["calibration"][lab] = {"n": c[0], "fair": c[1] / c[0], "price": c[2] / c[0],
                                       "hit": c[3] / c[0]}
    return out


def _q(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    return s[min(len(s) - 1, int(p * len(s)))]


def flow_context(stats: list, runs: list) -> dict:
    """The logger's own counts: minutes covered, the slice's RFQs and the whole
    tape's combo prints, and the share of the slice's combo RFQs that printed."""
    secs = sum(_f(s.get("secs"), 0.0) for s in stats)
    tot = collections.Counter()
    for s in stats:
        for k in ("rfq", "rfq_combo", "matched", "mve_prints"):
            tot[k] += int(s.get(k) or 0)
        tot["mve_usd"] += _f(s.get("mve_usd"), 0.0)
    shard = next((s.get("shard") for s in stats if s.get("shard")), [SHARD_FACTOR, 0])
    sf = int(shard[0] or 1)
    return {"minutes": secs / 60.0, "shard_factor": sf,
            "rfq_per_s_full": (tot["rfq"] * sf / secs) if secs else None,
            "combo_prints_per_s": (tot["mve_prints"] / secs) if secs else None,
            "combo_usd_per_hour": (tot["mve_usd"] / secs * 3600) if secs else None,
            "exec_share": (tot["matched"] / tot["rfq_combo"]) if tot["rfq_combo"] else None,
            "disconnects": sum(1 for r in runs if r.get("k") == "disc"),
            "matched": tot["matched"]}


# ---- the report ---------------------------------------------------------------

def usd(x, d=0):
    if x is None:
        return "n/a"
    return ("-" if x < 0 else "+") + f"${abs(x):,.{d}f}"


def pct(x, d=1):
    return "n/a" if x is None else f"{x * 100:+.{d}f}%"


def render(rep: dict, ctx: dict, since, until) -> str:
    fl, L = rep["flow"], []
    L.append(f"# Combo maker shadow: {since} to {until} (UTC days of the RFQs)")
    L.append("")
    L.append(f"Logger: {ctx['minutes']:,.0f} min on a 1/{ctx['shard_factor']} RFQ slice"
             f" ({ctx['disconnects']} disconnects). The full feed ran ~{ctx['rfq_per_s_full'] or 0:,.0f}"
             f" RFQs/s; combos printed {ctx['combo_prints_per_s'] or 0:,.1f}/s,"
             f" {usd(ctx['combo_usd_per_hour']).lstrip('+')} of taker premium an hour;"
             f" {pct(ctx['exec_share'], 2).lstrip('+')} of the slice's combo RFQs executed.")
    L.append(f"Scored {rep['n_scored']:,} executions ({rep['n_settled']:,} settled,"
             f" {rep['n_pending']:,} pending); eligible for the shadow: {rep['n_eligible']:,}"
             f" ({rep['n_eligible_settled']:,} settled). Why not eligible: "
             + ", ".join(f"{k} {v:,}" for k, v in sorted(rep["why"].items(), key=lambda kv: -kv[1])) + ".")
    L.append("")
    if not rep["n_settled"]:
        L.append(f"**No scored combo has settled yet** ({rep['n_pending']:,} pending): the P&L tables"
                 " wait for the settlements. The markup table needs none.")
        L.append("")
        return "\n".join(L + _s1_lines(rep) + book_lines.render(rep.get("book") or {})
                         + _markup_lines(rep) + _method_lines(rep)) + "\n"
    L.append("## The flow: what today's makers made at their own prices (settled)")
    L.append("")
    L.append(f"Makers took **{usd(fl['gross'])}** on {usd(fl['prem']).lstrip('+')} of taker premium"
             f" (hold {pct(fl['hold'])}), {usd(fl['net'])} after the assumed maker fee;"
             f" 90% range {usd(fl['ci'][0])} to {usd(fl['ci'][1])} (resampling {rep['ci_by']}s).")
    L.append("")
    L.append("| slice | n | taker premium | makers' P&L | hold | after fee |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for name, f in list(rep["flow_by"].items()) + [(f"{k} legs", v) for k, v in rep["flow_legs"].items()]:
        if f["n"]:
            L.append(f"| {name} | {f['n']:,} | {usd(f['prem']).lstrip('+')} | {usd(f['gross'])} | "
                     f"{pct(f['hold'])} | {usd(f['net'])} |")
    L.append("")
    L.append("## The shadow maker: independence fair + markup, pre-game, no same-game legs")
    L.append("")
    L.append("| markup | wins / eligible | our premium | P&L | fee | net | net / premium | 90% range | pending wins |")
    L.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rep["shadow"]:
        L.append(f"| {r['markup']:.0%} | {r['wins']:,} / {r['eligible']:,} | {usd(r['prem']).lstrip('+')} | "
                 f"{usd(r['gross'])} | {usd(-r['fee'])} | **{usd(r['net'])}** | {pct(r['per_prem'])} | "
                 f"{usd(r['ci'][0])} to {usd(r['ci'][1])} | {r['pending_wins']:,} |")
    L.append("")
    if any(r["fee_nfl"] > 0 for r in rep["shadow"]):
        L.append("Of that fee, on all-NFL combos with no same-game legs (exempt if that is what Kalshi's"
                 " \"uncorrelated NFL\" means): "
                 + ", ".join(f"{r['markup']:.0%} {usd(r['fee_nfl']).lstrip('+')}" for r in rep["shadow"]) + ".")
        L.append("")
    L.append("Net by leg count:")
    L.append("")
    L.append("| markup | " + " | ".join(lab for _, lab in LEG_BUCKETS) + " |")
    L.append("|---:|" + "---:|" * len(LEG_BUCKETS))
    for r in rep["shadow"]:
        L.append(f"| {r['markup']:.0%} | " + " | ".join(usd(r["by_legs"][lab]) for _, lab in LEG_BUCKETS) + " |")
    L.append("")
    L += _s1_lines(rep)
    L += book_lines.render(rep.get("book") or {})
    L += _markup_lines(rep)
    L.append("## Calibration (eligible, settled, takers buying YES)")
    L.append("")
    L.append("| fair bucket | n | mean fair | mean price paid | hit rate |")
    L.append("|---|---:|---:|---:|---:|")
    for lab, c in rep["calibration"].items():
        L.append(f"| {lab} | {c['n']:,} | {c['fair']:.2%} | {c['price']:.2%} | {c['hit']:.2%} |")
    L.append("")
    return "\n".join(L + _method_lines(rep)) + "\n"


def _s1_lines(rep: dict) -> list:
    s1 = rep.get("s1") or {}
    L = ["## Strategy 1, the live pilot's rule (main lines, 3-8 legs, fair 2-40c, legs <= 3c wide,"
         " >= 30 min to the start)", "",
         f"{s1.get('eligible', 0):,} executions fit ({s1.get('settled', 0):,} settled;"
         f" {s1.get('with_nfl', 0):,} all-NFL, which Kalshi's table exempts from the maker fee).", ""]
    if s1.get("rows"):
        L += ["| markup | wins | our premium | net after fee | net / premium | 90% range | pending wins |",
              "|---:|---:|---:|---:|---:|---:|---:|"]
        for r in s1["rows"]:
            L.append(f"| {r['markup']:.0%} | {r['wins']:,} | {usd(r['prem']).lstrip('+')} | "
                     f"**{usd(r['net'])}** | {pct(r['per_prem'])} | {usd(r['ci'][0])} to "
                     f"{usd(r['ci'][1])} | {r['pending_wins']:,} |")
    return L + [""]


def _markup_lines(rep: dict) -> list:
    L = ["## What the market charges over the independence fair (takers buying YES, eligible)", "",
         "| legs | n | 25th pct | median | 75th pct | priced below fair |",
         "|---|---:|---:|---:|---:|---:|"]
    for lab, v in rep["markup"].items():
        L.append(f"| {lab} | {v['n']:,} | {pct(v['p25'], 0)} | {pct(v['median'], 0)} | "
                 f"{pct(v['p75'], 0)} | {v['below_fair']:.0%} |")
    if not rep["markup"]:
        L.append("| (no eligible execution yet) | | | | | |")
    return L + [""]


def _method_lines(rep: dict) -> list:
    return [
        "## Method and caveats", "",
        f"- Fair = product of leg mids from the last 1-minute candle that ended before the RFQ;"
        f" legs wider than {MAX_LEG_SPREAD:.0%} are ineligible. Games: event ticker after the"
        f" series; start = earliest expected expiration - {GAME_HOURS:g} h.",
        "- A win needs a strictly better price than the print; ties and RFQs that never executed"
        " are not counted, so the win counts are a floor and our real fill rate is unknown.",
        f"- Fee: {MAKER_FEE_RATE} x contracts x p(1-p) on every fill, Kalshi's combo maker rate (50% of"
        " the 0.07 taker rate, kalshi.com/fee-schedule). The table excludes \"uncorrelated NFL\""
        " combos without defining them, so they are charged here too.",
        "- Combos on one night share games, so outcomes move together"
        + ("." if rep["ci_by"] == "day" else
           f": with under {BOOT_MIN_DAYS} days the range resamples anchor games and is too narrow."),
        "- Not modelled: our latency (makers answer in ~0.2 s), the 3 s confirm window, and"
        " requesters who steer flow to the best maker. Read-only; nothing was quoted."]


def book_fair(dir_, since, until, sel, markets, book, get=None) -> dict:
    """The second fair: the logger's sportsbook snapshots, plus lines read now
    for the games they miss, against each game-winner leg's Kalshi mid and
    result (combo_book_lines). Never raises: a failed read leaves it empty."""
    try:
        snaps = book_lines.load(dir_, since, until)
        needed = set()
        for e in sel:
            for leg in e["legs"]:
                ser = leg[0].split("-")[0]
                if ser in book_lines.LEAGUES:
                    ev = (markets.get(leg[0]) or {}).get("event_ticker") or "-".join(leg[0].split("-")[:2])
                    d = book_lines.day_of_event(ev)
                    if d:
                        needed.add((ser, d))
        later = book_lines.close_rows(needed, **({"get": get} if get else {}), log=log)
        lines = book_lines.Lines(snaps, later)
        out = book_lines.summarize(book_lines.calibration(sel, markets, book, lines))
        out["snap_rows"], out["later_rows"] = len(snaps), len(later)
        return out
    except Exception as e:
        log(f"! book fair: {type(e).__name__}: {str(e)[:200]}")
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    today = datetime.now(timezone.utc).date()
    ap.add_argument("--dir", default=DEFAULT_OUT)
    ap.add_argument("--since", default=(today - timedelta(days=7)).isoformat())
    ap.add_argument("--until", default=today.isoformat())
    ap.add_argument("--per-day", type=int, default=MAX_EXECS_PER_DAY)
    a = ap.parse_args(argv)
    since, until = date.fromisoformat(a.since), date.fromisoformat(a.until)
    execs, _rfqs, stats, runs = load(a.dir, since, until)
    if not execs:
        log(f"no executions logged {since}..{until} in {a.dir}")
        return 1
    sel = pick(execs, a.per_day)
    log(f"{len(execs):,} executions logged; scoring {len(sel):,}")
    from crypto_touch_mm import build_client
    reader = Reader(build_client())
    markets, book = fetch(reader, sel)
    starts = game_starts(markets)
    scored = [score_exec(e, markets, book, starts) for e in sel]
    rep = build(scored)
    rep["book"] = book_fair(a.dir, since, until, sel, markets, book)
    ctx = flow_context(stats, runs)
    text = render(rep, ctx, since, until)
    stamp = datetime.now(et_zone()).strftime("%Y-%m-%d")
    path = os.path.join(a.dir, f"combo_shadow_report_{stamp}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    with open(path[:-3] + ".json", "w", encoding="utf-8") as f:
        json.dump({"report": rep, "context": ctx, "since": str(since), "until": str(until),
                   "calls": reader.calls}, f, indent=1, default=str)
    log(f"{reader.calls} Kalshi reads; wrote {path}")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
