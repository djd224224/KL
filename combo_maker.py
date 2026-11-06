#!/usr/bin/env python3
r"""combo_maker.py -- a SMALL, hard-capped LIVE pilot of combo maker Strategy 1
(Jack 2026-11-05: "want to test something small starting tonight, that
includes thursday night football, based on strategy 1").

STRATEGY 1, the wide-markup book on different-game parlays. It answers an RFQ
only when:
  - every leg is a main line (game winner / spread / total) of a US league
    (MAIN_SERIES), and no two legs are on one game;
  - every game is at least MIN_LEAD_MIN from its start (Kalshi sets game
    markets to expire ~GAME_HOURS after the start: JAC@BAL 11/05 01:15Z ->
    04:15Z);
  - every leg settles within MAX_DAYS_OUT (its expected expiration), so a
    ticket's cash comes back within days;
  - MIN_LEGS-MAX_LEGS legs, each on a two-sided book no wider than
    MAX_LEG_SPREAD;
  - the fair price (the product of the legs' mids, 1 - mid for a NO leg) is in
    [MIN_FAIR, MAX_FAIR].
It only SELLS the parlay's YES, at fair x (1 + MARKUP) rounded up to the combo
grid: no_bid = 1 - that price, yes_bid "0" (the other side declined).

LAST LOOK: when a requester accepts, Kalshi gives a combo maker 3 s to confirm.
The legs are read again, fresh. It confirms only if every filter still passes,
our price is still >= fair x (1 + CONFIRM_MIN_EDGE), and every cap holds.

CAPS, checked before a quote and again at the confirm:
  one ticket's payout (contracts x $1)              <= MAX_PAYOUT
  what one ticket can lose (contracts x NO px)       <= MAX_TICKET_LOSS (if set)
  the payout on any one game, over open tickets      <= MAX_GAME_PAYOUT
  what the open tickets can lose (contracts x NO px) <= MAX_OPEN_LOSS
  confirmed tickets an ET day                        <= MAX_TICKETS
  quotes a minute                                    <= MAX_QUOTES_PER_MIN
The exchange adds its own: only the combo shard's cash (shard 1) can back a
fill, so what Jack moves there is the most the pilot can ever put at risk.

SAFETY: nothing is sent without --live (DRY prices and logs the quotes it
would send). A ticket counts as filled only once Kalshi's order says so: the
maker reads each confirmed ticket's order until it is final and books the
contracts and fee Kalshi recorded; nothing filled = "unfilled", which frees
its caps (quote_executed alone is not a fill: 11/06 03:04Z Kalshi canceled
one such order 5 s later with 0 filled). An order that bought YES, or a YES
position read back after one of the first fills, is the wrong direction: it
writes the STOP file and stops. STOP file:
run-logs\combo-maker\combo_maker.stop (it also refuses to start while the
file exists). One instance at a time (combo_maker.lock). Open tickets persist
in maker_state.json, so a restart keeps counting them against the caps;
settlements are read every SETTLE_EVERY_S and free their exposure.

Logs: run-logs\combo-maker\combo_<UTC date>.jsonl (quote / accept / confirm /
decline / executed / filled / unfilled / position / settled / stat / err) and
the console.

  python combo_maker.py                      # DRY: price and log, send nothing
  python combo_maker.py --live               # quote for real (Jack starts it)
  python combo_maker.py --live --minutes 120 # ...for two hours
Knobs: --markup, --max-payout, --max-game-payout, --max-open-loss,
--max-tickets (see --help). Jack funds the combo shard by hand first:
  python kalshi_subaccount.py shard 300 --to 1
"""
import argparse
import json
import math
import os
import socket
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import kalshi_ws  # noqa: E402
from combo_rfq_logger import WS_PATH, WS_URL, DayWriter, iso_ms, legs_of  # noqa: E402
from combo_rfq_score import (DEFAULT_GRID, MAIN_SERIES, grid_up, league_of, maker_fee,  # noqa: E402
                             sport_group)

OUT_DIR = os.environ.get("COMBO_MAKER_DIR", os.path.join(HERE, "run-logs", "combo-maker"))
STATE_NAME = "maker_state.json"
STOP_NAME = "combo_maker.stop"
LOCK_NAME = "combo_maker.lock"
# MAIN_SERIES comes from combo_rfq_score, so the shadow's Strategy 1 table and
# this pilot share one definition: US-league game / spread / total, and since
# 11/05 night the top soccer leagues (start rule checked) plus tennis, UFC,
# boxing and the college first half (not checked: an hour's more margin)
GAME_HOURS = 3.0
# Expected expiration minus kickoff, by series prefix, where it is not 3.0 h.
# NCAAF (ESPN check 11/06 04:45Z, 49 games): 3.0 h on 24, but 3.5 / 5 / 5.5 /
# 6.0 h on others, so its start is taken as expiry - 6 h (the latest seen):
# quoting stops >= 3 h before a 3.0-h game's kickoff, never after one's start.
# NFL 24/24, NBA, NHL, NCAAMB, NCAAWB and soccer checked at 3.0 h.
# UFC (11/06): Kalshi's fight expiries do not time the card (the Cortes Acosta
# vs Walker card expires 11/07 04:00-05:00Z; ESPN has it 11/07 22:00Z prelims,
# 11/08 01:00Z main card), so its start is taken as expiry - 8 h: a whole card
# before any expiry set after the main event.
START_HOURS = {"KXNCAAF": 6.0, "KXUFCFIGHT": 8.0}
ALL_SPORTS = ("NFL", "NBA", "WNBA", "NHL", "MLB", "NCAAF", "NCAAMB", "NCAAWB", "SOCCER", "UFC", "BOXING",
              "TENNIS")
# Jack 2026-11-05 ~11:45 PM ET: "quote NFL soccer, NBA, NHL college basketball NCAAF, UFC"
DEFAULT_SPORTS = ("NFL", "NBA", "NHL", "NCAAMB", "NCAAWB", "NCAAF", "SOCCER", "UFC")
MOVE_WINDOW_S = 600.0               # a leg whose mid moved >= cfg.max_leg_move inside this window...
MOVE_HOLD_S = 1800.0                # ...is treated as in play for this long
SHARD_FACTOR, SHARD_KEY = 50, 1     # a 2% slice of RFQs (the logger reads key 0)
STALE_SECS = 30.0
SETTLE_EVERY_S = 300.0
STAT_SECS = 60.0
QUOTE_TTL_S = 120.0                 # forget our quote this long after it was sent
DIRECTION_CHECKS = 3                # read back the position of the first N fills
READ_GAP_S = 0.05                   # the loop's client: calls >= 50 ms apart (reads can't burst
                                    # the read budget the IMM shares), sleeping only what is left
FILL_CHECK_AFTER_S = 3.0            # read a ticket's order this long after quote_executed...
FILL_CHECK_EVERY_S = 5.0            # ...and again this often until Kalshi calls it final
NO_ORDER_AFTER_S = 60.0             # confirmed, no order on Kalshi after this long: nothing traded
DIRECTION_TRIES = 5                 # ...up to this many reads each
ET_OFFSET = None                    # filled by et_today()


class Config:
    def __init__(self, **kw):
        self.live = False
        self.markup = 0.10                  # on a 2-leg parlay; more legs add markup_per_leg each
        # MORE LEGS, MORE MARKUP (Jack 2026-11-05: "quote a higher spread, the more
        # legs"). The market's own markup over the leg mids climbs with the legs:
        # fee-paying cross-game pre-game prints (shadow, n 819), median / p75 2 legs
        # +1.9/+2.5%, 3 +2.3/+3.6%, 4 +3.1/+4.2%, 5 +3.7/+5.8% -- about a point a leg
        # at the p75 -- and each leg's mid error compounds into the fair
        self.markup_per_leg = 0.01
        self.confirm_min_edge = None        # None = half the markup (0.05 at the default 10%)
        # ALL-NFL CUT (Jack 2026-11-06: "NFL parlays require 2c less edge because there
        # are no maker fees"): a parlay whose every leg is an NFL market pays no maker
        # fee (fee_free; Kalshi's table: "Combos (excluding uncorrelated NFL)"), and the
        # fee is 0.035 p(1-p), 2.1-3.4% of our price over the 2-40c fairs. So it needs
        # this much less edge at the quote AND at the last look (the fee saved is the
        # whole fee at both): 0.02 quotes 2 legs at 2% instead of 4% and confirms them
        # down to 0% instead of 2%. 0 here, so a bare Config (the tests, the shadow
        # scorer) prices every parlay alike; the CLI's default is 0.02
        self.nfl_markup_cut = 0.0
        # NFL ONLY (Jack 2026-11-05, after the shadow: 147 Strategy-1 prints sat a
        # median +1.8% over fair, 3% of them over +5%): every leg an NFL main
        # line, which Kalshi's fee table exempts from the combo maker fee
        # ("Combos (excluding uncorrelated NFL)"), so a 3% markup is all edge
        self.nfl_only = False
        self.shard_factor = SHARD_FACTOR    # see 1/N of the RFQs (50 = 2%, ~90 KB/s; 10 = 10%, ~470 KB/s)
        # GAMES (Jack 2026-11-05: "want to start quoting tonight, with the 2 NBA
        # games as components"): only parlays with a leg on one of these game
        # keys (26NOV05MIAGSW), or with every leg on them (games_mode "all")
        self.games = None
        self.games_mode = "any"
        self.min_legs, self.max_legs = 3, 8
        self.min_fair, self.max_fair = 0.02, 0.40
        self.max_leg_spread = 0.03
        self.min_lead_min = 30.0
        self.max_payout = 150.0
        # ONE TICKET'S LOSS (Jack 2026-11-05 ~11:05 PM ET: "allow a single loss of up
        # to $300"): contracts x (1 - our price), what the ticket loses if the
        # parlay hits. None = only max_payout limits a ticket (a $150 payout loses
        # under $150); set it with a max_payout above it, or the payout cap binds first
        self.max_ticket_loss = None
        self.min_premium = 0.50
        self.max_game_payout = 300.0
        self.max_open_loss = 400.0
        self.max_tickets = 30
        self.max_quotes_per_min = 60
        # QUOTE SENDERS (11/06 04:05Z, at 150 quotes a minute: one loop did every
        # POST (~270 ms: ~170 on the wire + the client's flat 100 ms sleep) and every
        # leg read (~133 ms, the same sleep), and RFQs reached it 1.2 s old). Quotes
        # now go out from this many threads, each with its own client; the loop only
        # reads and prices. 0 = send inline (the tests)
        self.quote_workers = 0
        self.max_reads_per_min = 120        # leg reads (10 tokens each): ~20 tokens/s at most
        # the sports whose start rule (expiry - 3 h) was NOT checked against a
        # schedule (tennis, UFC, boxing, the college first half): at least this
        # much margin, so an offset up to an hour longer still stops before play
        self.unchecked_lead_min = 60.0
        # SPORTS (11/06 04:21-04:33Z: 6 tickets sold on ITF women's matches already
        # in play -- their markets expire ~6 h after a match starts, not 3, so
        # "expiry - 3 h" put the start 2-3 h late and the buyers had the score).
        # Every leg's sport must be on this list (sport_of); tennis and boxing are off
        self.sports = set(DEFAULT_SPORTS)
        # IN-PLAY GUARD, any sport: a leg whose mid (from our own reads) moved by
        # this much inside MOVE_WINDOW_S is held out for MOVE_HOLD_S, at the quote
        # and at the last look (the ITF legs swung 15-50c while we sold)
        self.max_leg_move = 0.05
        # MAKER ONLY (Jack 2026-11-06: "it should only ever be a maker"). A confirmed
        # quote becomes an ordinary limit order on the combo's own book (~1.7 s after
        # the confirm), so it TAKES any resting order that crosses it: 11/06 04:25 and
        # 04:33Z two of ours filled as the taker against a resting YES bid 0.6-1c over
        # our price (taker fee 0.07 p(1-p), twice the maker's). The last look reads
        # that book and declines when a resting YES bid is at or over our YES price
        self.maker_only = True
        # HORIZON (Jack 2026-11-05 ~11 PM ET: "dont quote anything further than 4
        # days out, to not tie up cash."): every leg's expected expiration (when it
        # settles, and the ticket's cash comes back) within this many days; 0 = off
        self.max_days_out = 4.0
        # EXTRAS (Jack 2026-11-05: first-half / quarter / team-total lines, and
        # props on different games, "only where both sides of the book have real
        # depth ($1000+)"): US leagues; $ at the best bid AND the best ask (size x
        # price) >= min_depth_usd at the quote and again at the last look; the
        # start is the game's (its winner market's expiry - 3 h), not the leg's
        self.allow_extras = True
        self.min_depth_usd = 1000.0
        self.max_rfq_age_ms = 1500.0        # an RFQ older than this when we get to it: skip
        self.leg_cache_s = 15.0
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"unknown setting {k}")
            setattr(self, k, v)
        if self.confirm_min_edge is None:
            self.confirm_min_edge = self.markup / 2.0

    def nfl_cut(self, legs) -> float:
        """The all-NFL cut for these legs (0 without legs, or with a non-NFL leg)."""
        return self.nfl_markup_cut if (legs and self.nfl_markup_cut and self.fee_free(legs)) else 0.0

    def markup_for(self, n_legs: int, legs=None) -> float:
        """The markup on an n-leg parlay: `markup` at 2 legs, + `markup_per_leg`
        for each leg over 2; nfl_markup_cut less when every one of `legs` is NFL."""
        m = self.markup + self.markup_per_leg * max(0, int(n_legs) - 2)
        return max(0.0, m - self.nfl_cut(legs))

    def confirm_edge_for(self, n_legs: int, legs=None) -> float:
        """The last look's minimum edge on an n-leg parlay: confirm_min_edge at 2
        legs, + half the step for each leg over 2 (half the markup, as before);
        the whole all-NFL cut less when every one of `legs` is NFL (floor 0: our
        price at or over the fair)."""
        e = self.confirm_min_edge + self.markup_per_leg / 2.0 * max(0, int(n_legs) - 2)
        return max(0.0, e - self.nfl_cut(legs))

    def fee_free(self, legs) -> bool:
        """Every leg an NFL market (main lines on different games by the
        filters): the fee table's "uncorrelated NFL" combos, no maker fee."""
        return all(str(l[0]).startswith("KXNFL") for l in legs)


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} [MAKER] {msg}", flush=True)


def _f(x, dflt=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return dflt


def _iso_s(s):
    ms = iso_ms(s)
    return None if ms is None else ms / 1000.0


def et_day(ts: float) -> str:
    try:
        import pytz
        return datetime.fromtimestamp(ts, pytz.timezone("US/Eastern")).strftime("%Y-%m-%d")
    except ImportError:
        return datetime.fromtimestamp(ts - 5 * 3600, timezone.utc).strftime("%Y-%m-%d")


def game_of(ticker: str) -> str:
    """A main line's game: the ticker's segment after the series
    (KXNBASPREAD-26NOV05MIAGSW-GSW11 -> 26NOV05MIAGSW)."""
    parts = str(ticker).split("-")
    return parts[1] if len(parts) > 1 else str(ticker)


# ---- the decision, free of the network --------------------------------------------

def is_extra(ticker: str) -> bool:
    return sport_group(str(ticker).split("-")[0]) in ("extra", "prop")


def pre_check(legs: list, cfg: Config):
    """Why an RFQ's legs are out before any read (None = worth pricing)."""
    if not (cfg.min_legs <= len(legs) <= cfg.max_legs):
        return "legs"
    if any(len(l) < 2 or l[1] not in ("yes", "no") or len(l) > 2 for l in legs):
        return "side"                                   # a scalar leg too
    if any(not (MAIN_SERIES.match(str(l[0]).split("-")[0]) or (cfg.allow_extras and is_extra(l[0])))
           for l in legs):
        return "not_main"
    if any(sport_of(str(l[0]).split("-")[0]) not in cfg.sports for l in legs):
        return "sport_off"
    if cfg.nfl_only and not cfg.fee_free(legs):
        return "not_nfl"
    games = [game_of(l[0]) for l in legs]
    if len(set(games)) < len(games):
        return "same_game"
    if cfg.games:
        on = [g in cfg.games for g in games]
        if not (all(on) if cfg.games_mode == "all" else any(on)):
            return "not_games"
    return None


def price_check(legs: list, markets: dict, now_s: float, cfg: Config, game_start=None):
    """(why not, fair): every leg active, two-sided, no wider than the cap,
    its game far enough from the start; the fair in range. An extra / prop leg
    also needs min_depth_usd at both its best bid and best ask, and its start
    comes from `game_start` {game key: start} (its game's winner market)."""
    probs = []
    for tk, side in ((l[0], l[1]) for l in legs):
        m = markets.get(tk)
        if not m:
            return "no_market", None
        if str(m.get("status") or "") != "active":
            return "not_active", None
        bid, ask = _f(m.get("yes_bid_dollars"), 0.0), _f(m.get("yes_ask_dollars"), 1.0)
        if not (0.0 < bid < ask < 1.0):
            return "one_sided", None
        if ask - bid > cfg.max_leg_spread + 1e-9:
            return "wide", None
        if too_far(_iso_s(m.get("expected_expiration_time")), now_s, cfg):
            return "too_far", None
        if is_extra(tk):
            if (_f(m.get("yes_bid_size_fp"), 0.0) * bid < cfg.min_depth_usd
                    or _f(m.get("yes_ask_size_fp"), 0.0) * ask < cfg.min_depth_usd):
                return "thin", None
            start = (game_start or {}).get(game_of(tk))
            if start is None:
                return "no_game_start", None
        else:
            exp = _iso_s(m.get("expected_expiration_time"))
            if exp is None:
                return "no_start", None
            start = start_of(tk, exp)
        if start - now_s < lead_min(tk, cfg) * 60:
            return "starts_soon", None
        mid = (bid + ask) / 2.0
        probs.append(mid if side == "yes" else 1.0 - mid)
    fair = math.prod(probs)
    if not (cfg.min_fair <= fair <= cfg.max_fair):
        return "fair_range", fair
    return None, fair


def sport_of(series: str) -> str:
    """'KXNBAPTS' -> 'NBA', 'KXEPLGAME' -> 'SOCCER', 'KXUFCFIGHT' -> 'UFC',
    'KXITFWMATCH' -> 'TENNIS' ('' outside the quotable series)."""
    g = sport_group(series)
    if g == "soccer":
        return "SOCCER"
    if g in ("us", "extra", "prop"):
        return league_of(series) or ""
    if g == "unchecked":
        if series.startswith("KXUFCFIGHT"):
            return "UFC"
        if series.startswith("KXBOXING"):
            return "BOXING"
        return "TENNIS"
    return ""


def start_of(ticker: str, exp_s: float) -> float:
    """A game's start from its market's expected expiration: expiry - 3 h,
    or the league's own offset (START_HOURS)."""
    series = str(ticker).split("-")[0]
    hours = next((h for p, h in START_HOURS.items() if series.startswith(p)), GAME_HOURS)
    return exp_s - hours * 3600


def too_far(settles_s, now_s: float, cfg: Config) -> bool:
    """A leg that settles (expected expiration) more than max_days_out from now."""
    return bool(cfg.max_days_out) and settles_s is not None and settles_s - now_s > cfg.max_days_out * 86400


def best_yes_bid(book: dict):
    """The best resting YES bid on a market's book (GET /markets/{t}/orderbook:
    orderbook_fp.yes_dollars [[price, size], ...], or the cents form), None if empty."""
    ob = (book or {}).get("orderbook_fp") or {}
    levels = [_f(x[0]) for x in ob.get("yes_dollars") or [] if x and _f(x[0]) is not None and _f(x[-1], 0) > 0]
    if not levels:
        old = (book or {}).get("orderbook") or {}
        levels = [x[0] / 100.0 for x in old.get("yes") or [] if x and x[0] is not None and (x[-1] or 0) > 0]
    return max(levels) if levels else None


def would_take(book: dict, ask: float) -> bool:
    """Our order buys NO at 1 - ask: a resting YES bid at >= ask is a NO offer at
    <= our bid, so the order would take it instead of being taken."""
    b = best_yes_bid(book)
    return b is not None and b >= ask - 1e-9


def lead_min(ticker: str, cfg: Config) -> float:
    """Minutes before the start a leg stops being quotable: the configured
    margin, and at least cfg.unchecked_lead_min for the unchecked sports."""
    if sport_group(str(ticker).split("-")[0]) == "unchecked":
        return max(cfg.min_lead_min, cfg.unchecked_lead_min)
    return cfg.min_lead_min


def quote_price(fair: float, cfg: Config, grid=None, n_legs: int = 2, legs=None):
    """(our YES price, the no_bid we post): fair x (1 + the n-leg markup) rounded UP
    (the all-NFL cut off it when `legs` are all NFL)."""
    ask = grid_up(fair * (1.0 + cfg.markup_for(n_legs, legs)), grid or DEFAULT_GRID)
    return ask, round(1.0 - ask, 4)


def contracts_of(rfq: dict, ask: float):
    """Contracts the RFQ would fill at our price: a target-cost RFQ buys
    target / price; a contract-sized one its contracts."""
    tc = _f(rfq.get("tc"))
    if tc and tc > 0:
        return tc / ask
    return _f(rfq.get("cn"), 0.0) or 0.0


class Book:
    """Our tickets (persisted): open ones count against the caps until their
    combo settles."""

    def __init__(self, path: str = None):
        self.path = path
        self.tickets = []
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                self.tickets = json.load(f).get("tickets") or []

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"tickets": self.tickets}, f, indent=1)
        os.replace(tmp, self.path)

    def open(self):
        return [t for t in self.tickets if t.get("status") in ("confirmed", "executed")]

    def game_payout(self, game: str) -> float:
        return sum(t["contracts"] for t in self.open() if game in t["games"])

    def open_loss(self) -> float:
        return sum(t["contracts"] * (1.0 - t["ask"]) for t in self.open())

    def day_count(self, day: str) -> int:
        return sum(1 for t in self.tickets if t.get("day") == day
                   and t.get("status") in ("confirmed", "executed", "settled"))

    def add(self, t: dict) -> None:
        self.tickets.append(t)
        self.save()


def caps_check(book: Book, games: list, contracts: float, ask: float, cfg: Config, day: str):
    """Why a ticket of `contracts` at `ask` would break a cap (None = fits)."""
    if contracts * ask < cfg.min_premium:
        return "dust"
    if contracts > cfg.max_payout + 1e-9:
        return "payout_cap"
    if cfg.max_ticket_loss is not None and contracts * (1.0 - ask) > cfg.max_ticket_loss + 1e-9:
        return "ticket_loss_cap"
    for g in games:
        if book.game_payout(g) + contracts > cfg.max_game_payout + 1e-9:
            return "game_cap"
    if book.open_loss() + contracts * (1.0 - ask) > cfg.max_open_loss + 1e-9:
        return "loss_cap"
    if book.day_count(day) >= cfg.max_tickets:
        return "ticket_cap"
    return None


def confirm_check(q: dict, markets: dict, now_s: float, cfg: Config, book: Book, contracts: float,
                  game_start=None):
    """The last look: (why not, fair now)."""
    why, fair = price_check(q["legs"], markets, now_s, cfg, game_start)
    if why:
        return why, fair
    if q["ask"] < fair * (1.0 + cfg.confirm_edge_for(len(q["legs"]), q["legs"])) - 1e-9:
        return "edge_gone", fair
    return caps_check(book, q["games"], contracts, q["ask"], cfg, et_day(now_s)), fair


# ---- the network side -------------------------------------------------------------

def put_json(client, path: str, body: dict = None):
    """A signed PUT through the bots' REST client (it has none of its own).
    Returns the JSON body, or {} for a 204."""
    client.rate_limit()
    resp = client._http().put(client.host + path, data=json.dumps(body or {}),
                              headers=client.request_headers("PUT", path),
                              timeout=client.http_timeout)
    client.raise_if_bad_response(resp)
    return resp.json() if resp.content else {}


def lock_file(out_dir: str):
    f = open(os.path.join(out_dir, LOCK_NAME), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


class MinGap:
    """A client's pacing: calls at least `gap` s apart, sleeping only what is left
    of the gap (the bots' client sleeps a whole 100 ms whenever its last call was
    under 100 ms ago, which cost the maker ~100 ms a call)."""

    def __init__(self, gap: float, clock=time.monotonic, sleep=time.sleep):
        self.gap, self.clock, self.sleep = gap, clock, sleep
        self.last = -1e18
        self.lock = threading.Lock()

    def __call__(self) -> None:
        with self.lock:
            wait = self.last + self.gap - self.clock()
            if wait > 0:
                self.sleep(wait)
            self.last = self.clock()


class _Locked:
    """The day writer, shared by the loop and the quote senders."""

    def __init__(self, w):
        self.w, self.lock = w, threading.Lock()

    def write(self, o) -> None:
        with self.lock:
            self.w.write(o)

    def flush(self) -> None:
        with self.lock:
            self.w.flush()

    def __getattr__(self, k):
        return getattr(self.w, k)


class Maker:
    def __init__(self, client, cfg: Config, writer, book: Book, out_dir: str, clock=time.time,
                 client_factory=None):
        self.c = client
        self.cfg = cfg
        self.w = _Locked(writer)
        self.lock = threading.RLock()   # stats, quotes, pending, errors: shared with the senders
        self.pending = {}               # rfq id -> our quote while its POST is out
        self.client_factory = client_factory     # a client for each sender thread
        self._tl = threading.local()
        self.pool = (ThreadPoolExecutor(max_workers=cfg.quote_workers, thread_name_prefix="quote")
                     if cfg.live and cfg.quote_workers else None)
        self.book = book
        self.out_dir = out_dir
        self.clock = clock
        self.cache = {}                 # leg ticker -> (read at, market)
        self.quotes = {}                # quote id -> our quote
        self.sent = deque()             # quote send times (rate cap)
        self.reads = deque()            # leg read times (rate cap)
        self.exp = {}                   # leg ticker -> expected expiration (never changes)
        self.game_start = {}            # game key -> start (from its main lines' expiry - 3 h)
        self.mids = {}                  # leg ticker -> deque of (read at, mid): the in-play guard
        self.moving_until = {}          # leg ticker -> held out until
        self.st = {}
        self.direction_checks = 0
        self.pause_until = 0.0
        self.errors = 0
        self.last_settle = 0.0
        self._reset_stats()

    def _reset_stats(self):
        with self.lock:
            self.st = {"rfq": 0, "pre_ok": 0, "priced": 0, "quoted": 0, "dry": 0, "accepted": 0,
                       "confirmed": 0, "declined": 0, "executed": 0, "errors": 0, "why": {}}
            self.st_since = self.clock()

    def _why(self, w):
        with self.lock:
            self.st["why"][w] = self.st["why"].get(w, 0) + 1

    def close(self) -> None:
        """Let the quotes in flight finish (their ids and records) before a stop."""
        if self.pool is not None:
            self.pool.shutdown(wait=True)

    def stop_file(self, why: str) -> None:
        with open(os.path.join(self.out_dir, STOP_NAME), "w", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} {why}\n")
        log(f"!! STOP written: {why}")

    # ---- reads ------------------------------------------------------------------

    def markets(self, tickers, max_age_s: float) -> dict:
        now = self.clock()
        need = [t for t in tickers if t not in self.cache or now - self.cache[t][0] > max_age_s]
        if need:
            js = self.c.get("/markets", {"tickers": ",".join(sorted(set(need))), "limit": 1000})
            for m in js.get("markets") or []:
                if m.get("ticker"):
                    self.cache[m["ticker"]] = (now, m)
                    ex = _iso_s(m.get("expected_expiration_time"))
                    if ex is not None:
                        self.exp[m["ticker"]] = ex
                        if not is_extra(m["ticker"]) and sport_group(m["ticker"].split("-")[0]):
                            self.game_start[game_of(m["ticker"])] = start_of(m["ticker"], ex)
                    self._track_mid(m, now)
        return {t: self.cache[t][1] for t in tickers if t in self.cache}

    def _track_mid(self, m: dict, now: float) -> None:
        bid, ask = _f(m.get("yes_bid_dollars"), 0.0), _f(m.get("yes_ask_dollars"), 1.0)
        if not (0.0 < bid < ask < 1.0):
            return
        tk = m["ticker"]
        h = self.mids.setdefault(tk, deque())
        h.append((now, (bid + ask) / 2.0))
        while h and now - h[0][0] > MOVE_WINDOW_S:
            h.popleft()
        lo, hi = min(x for _, x in h), max(x for _, x in h)
        if self.cfg.max_leg_move and hi - lo >= self.cfg.max_leg_move - 1e-9:
            if self.moving_until.get(tk, 0.0) < now:
                log(f"in-play guard: {tk} moved {lo:.3f}-{hi:.3f} inside {MOVE_WINDOW_S / 60:.0f} min: "
                    f"held out {MOVE_HOLD_S / 60:.0f} min")
            self.moving_until[tk] = now + MOVE_HOLD_S

    def moving(self, legs, now: float) -> bool:
        return any(self.moving_until.get(l[0], 0.0) > now for l in legs)

    def learn_game_starts(self, legs, now) -> None:
        """An extra / prop leg's game start, from its game's winner market
        (one read a game, kept for the run)."""
        for tk in {l[0] for l in legs if is_extra(l[0])}:
            game = game_of(tk)
            if game in self.game_start:
                continue
            league = league_of(tk.split("-")[0])
            if not league:
                continue
            self.reads.append(now)
            js = self.c.get("/markets", {"event_ticker": f"KX{league}GAME-{game}", "limit": 10})
            for m in js.get("markets") or []:
                ex = _iso_s(m.get("expected_expiration_time"))
                if ex is not None:
                    self.game_start[game] = start_of(m["ticker"], ex)
                    break

    # ---- messages ---------------------------------------------------------------

    def handle(self, m: dict) -> None:
        typ, msg = m.get("type"), m.get("msg") or {}
        if typ == "rfq_created":
            self.on_rfq(msg)
        elif typ == "quote_accepted":
            self.on_accepted(msg)
        elif typ == "quote_executed":
            self.on_executed(msg)
        elif typ == "error":
            self.w.write({"k": "err", "t": self.clock(), "msg": msg})

    def on_rfq(self, msg: dict) -> None:
        self.st["rfq"] += 1
        legs = legs_of(msg.get("mve_selected_legs"))
        if not legs or not msg.get("id"):
            return
        why = pre_check(legs, self.cfg)
        if why:
            self._why(why)
            return
        self.st["pre_ok"] += 1
        now = self.clock()
        if now < self.pause_until:
            self._why("paused")
            return
        while self.sent and now - self.sent[0] > 60:
            self.sent.popleft()
        if len(self.sent) >= self.cfg.max_quotes_per_min:
            self._why("rate_cap")
            return
        # a leg whose game already starts too soon is known from an earlier
        # read: skip without spending one (11/05: most reads went to parlays
        # holding the in-play ORL@PHX)
        if self.moving(legs, now):
            self._why("moving")
            return
        for leg in legs:
            if too_far(self.exp.get(leg[0]), now, self.cfg):
                self._why("too_far")
                return
            if is_extra(leg[0]):
                st = self.game_start.get(game_of(leg[0]))
            else:
                ex = self.exp.get(leg[0])
                st = None if ex is None else start_of(leg[0], ex)
            if st is not None and st - now < lead_min(leg[0], self.cfg) * 60:
                self._why("starts_soon")
                return
        while self.reads and now - self.reads[0] > 60:
            self.reads.popleft()
        if len(self.reads) >= self.cfg.max_reads_per_min:
            self._why("read_cap")
            return
        created = iso_ms(msg.get("created_ts"))
        if created is not None and now * 1000.0 - created > self.cfg.max_rfq_age_ms:
            self._why("stale")                          # makers answer in ~0.2 s: too late to win
            return
        rfq = {"id": msg["id"], "mt": msg.get("market_ticker"), "legs": legs,
               "tc": msg.get("target_cost_dollars") if _f(msg.get("target_cost_dollars"), 0) > 0 else None,
               "cn": msg.get("contracts_fp"), "cr": str(msg.get("creator_id") or "")[:8],
               "rfq_ms": iso_ms(msg.get("created_ts"))}
        try:
            t0 = time.time()
            if any(l[0] not in self.cache or now - self.cache[l[0]][0] > self.cfg.leg_cache_s for l in legs):
                self.reads.append(now)
            mk = self.markets([l[0] for l in legs], self.cfg.leg_cache_s)
            self.learn_game_starts(legs, now)
            read_ms = (time.time() - t0) * 1000
        except Exception as e:
            self._error("leg read", e)
            return
        if self.moving(legs, now):                      # this read showed the move
            self._why("moving")
            return
        why, fair = price_check(legs, mk, now, self.cfg, self.game_start)
        if why:
            self._why(why)
            return
        self.st["priced"] += 1
        ask, no_bid = quote_price(fair, self.cfg, n_legs=len(legs), legs=legs)
        cts = contracts_of(rfq, ask)
        games = [game_of(l[0]) for l in legs]
        why = caps_check(self.book, games, cts, ask, self.cfg, et_day(now))
        if why:
            self._why(why)
            return
        q = dict(rfq, fair=fair, ask=ask, no_bid=no_bid, mu=round(self.cfg.markup_for(len(legs), legs), 4),
                 contracts=cts, games=games, at=now,
                 lag_ms=None if rfq["rfq_ms"] is None else now * 1000 - rfq["rfq_ms"], read_ms=read_ms)
        if not self.cfg.live:
            self.st["dry"] += 1
            self.sent.append(now)                       # DRY keeps LIVE's quote rate
            self.w.write(dict(q, k="would_quote"))
            return
        body = {"rfq_id": rfq["id"], "yes_bid": "0.0000", "no_bid": f"{no_bid:.4f}", "rest_remainder": False}  # "0" is refused: "invalid dollar precision"
        self.sent.append(now)                           # counts against the cap from now, out or not
        if self.pool is None:
            self._send(q, body, now)
            return
        with self.lock:
            self.pending[rfq["id"]] = q
        self.pool.submit(self._send_logged, q, body, now)

    def _sender_client(self):
        if self.pool is None or self.client_factory is None:
            return self.c
        c = getattr(self._tl, "c", None)
        if c is None:
            c = self._tl.c = self.client_factory()
        return c

    def _send_logged(self, q: dict, body: dict, now: float) -> None:
        try:
            self._send(q, body, now)
        except Exception as e:                       # a sender thread must never fail silently
            with self.lock:
                self.pending.pop(q["id"], None)
            self._error("sender", e)

    def _send(self, q: dict, body: dict, now: float) -> None:
        """POST one quote (inline, or on a sender thread) and book it."""
        try:
            js = self._sender_client().post(path="/communications/quotes", body=json.dumps(body))
        except Exception as e:
            with self.lock:
                self.pending.pop(q["id"], None)
            self._error("quote", e)
            return
        qid = js.get("id")
        with self.lock:
            self.pending.pop(q["id"], None)
            self.st["quoted"] += 1
            self.quotes[qid] = dict(q, quote_id=qid)
        self.w.write(dict(q, k="quote", quote_id=qid, sent_ms=(time.time() - now) * 1000))

    def on_accepted(self, msg: dict) -> None:
        qid = msg.get("quote_id")
        with self.lock:
            q = self.quotes.get(qid)
            if q is None and msg.get("rfq_id") in self.pending:    # accepted before our POST came back:
                q = dict(self.pending[msg["rfq_id"]], quote_id=qid)  # quote_accepted only reaches its maker
        if q is None:
            return                                  # not ours (another maker's quote filled)
        self.st["accepted"] += 1
        now = self.clock()
        cts = _f(msg.get("contracts_accepted_fp")) or q["contracts"]
        rec = {"k": "accept", "quote_id": qid, "rfq": q["id"], "mt": q["mt"],
               "side": msg.get("accepted_side"), "yes_bid": msg.get("yes_bid_dollars"),
               "no_bid": msg.get("no_bid_dollars"), "contracts": cts}
        self.w.write(rec)
        if _f(msg.get("no_bid_dollars")) is not None and abs(_f(msg.get("no_bid_dollars")) - q["no_bid"]) > 1e-6:
            self._decline(q, "price_mismatch", None, cts)
            return
        try:
            mk = self.markets([l[0] for l in q["legs"]], -1.0)      # always re-read: the last look
        except Exception as e:
            self._error("confirm read", e)
            self._decline(q, "read_failed", None, cts)
            return
        if self.moving(q["legs"], now):
            self._decline(q, "moving", None, cts)
            return
        why, fair_now = confirm_check(q, mk, now, self.cfg, self.book, cts, self.game_start)
        if why:
            self._decline(q, why, fair_now, cts)
            return
        if self.cfg.maker_only:
            try:
                ob = self.c.get(f"/markets/{q['mt']}/orderbook", {"depth": 10})
            except Exception as e:
                self._error("book read", e)
                self._decline(q, "book_read_failed", fair_now, cts)
                return
            if would_take(ob, q["ask"]):
                self._decline(q, "would_take", fair_now, cts)
                return
        try:
            put_json(self.c, f"/communications/rfqs/{q['id']}/quotes/{qid}/confirm")
        except Exception as e:
            self._error("confirm", e)
            self._decline(q, "confirm_failed", fair_now, cts)
            return
        self.st["confirmed"] += 1
        t = {"quote_id": qid, "rfq": q["id"], "mt": q["mt"], "legs": q["legs"], "games": q["games"],
             "ask": q["ask"], "fair": q["fair"], "fair_confirm": fair_now, "contracts": cts,
             "status": "confirmed", "day": et_day(now), "at": now,
             # all-NFL combos pay no maker fee by Kalshi's table: checked on the fills
             "fee_est": 0.0 if self.cfg.fee_free(q["legs"]) else maker_fee(cts, q["ask"])}
        self.book.add(t)
        self.w.write(dict(t, k="confirm", confirm_ms=(self.clock() - now) * 1000))
        log(f"CONFIRMED {q['mt']} {len(q['legs'])} legs: sold {cts:,.1f} YES at {q['ask']:.3f} "
            f"(fair {fair_now:.3f}), can lose ${cts * (1 - q['ask']):,.2f}")

    def _decline(self, q, why, fair_now, cts):
        self.st["declined"] += 1
        self.w.write({"k": "decline", "quote_id": q.get("quote_id"), "rfq": q["id"], "mt": q["mt"],
                      "why": why, "ask": q["ask"], "fair": q["fair"], "fair_now": fair_now,
                      "contracts": cts})
        log(f"declined {q['mt']}: {why}")

    def on_executed(self, msg: dict) -> None:
        qid = msg.get("quote_id")
        t = next((x for x in self.book.tickets if x.get("quote_id") == qid), None)
        if t is None:
            return
        self.st["executed"] += 1
        t["status"] = "executed"
        t["order_id"] = msg.get("order_id")
        t["executed_at"] = self.clock()
        self.book.save()
        self.w.write({"k": "executed", "quote_id": qid, "mt": t["mt"], "order_id": msg.get("order_id")})

    def check_fills(self, now: float) -> None:
        """Kalshi's word on every confirmed ticket. quote_executed is not proof
        of a fill (11/06 03:04Z: it came for an order Kalshi canceled 5 s later
        with 0 filled, and the maker booked a phantom ticket), so each ticket's
        order is read until it is final (executed / canceled). With no executed
        message, our orders on its combo are looked up instead. Then the ticket
        takes the contracts Kalshi filled and the fee it charged; nothing filled
        = "unfilled", out of the caps and the P&L. An order that bought YES is
        the wrong direction: STOP, exit 5."""
        for t in self.book.tickets:
            if t.get("status") not in ("confirmed", "executed") or (t.get("fill_checked") and t.get("px_checked")):
                continue                                    # (fill_checked without px_checked: booked before
                                                            # the fill price was read -- read it once)
            since = t.get("executed_at") or t.get("at") or now
            if now - since < FILL_CHECK_AFTER_S or now - t.get("fill_read_at", 0.0) < FILL_CHECK_EVERY_S:
                continue
            t["fill_read_at"] = now
            try:
                if t.get("order_id"):
                    o = (self.c.get(f"/portfolio/orders/{t['order_id']}", {}) or {}).get("order") or {}
                else:
                    js = self.c.get("/portfolio/orders", {"ticker": t["mt"],
                                                         "min_ts": int((t.get("at") or now) - 5), "limit": 20})
                    mine = [x for x in js.get("orders") or [] if x.get("ticker") == t["mt"]]
                    if not mine:
                        if now - (t.get("at") or now) >= NO_ORDER_AFTER_S:
                            self._book_fill(t, 0.0, "no order on Kalshi", None, None)
                        continue
                    o = mine[0]
                    t["order_id"] = o.get("order_id")
            except Exception as e:
                self._error("fill check", e)
                continue
            status = str(o.get("status") or "")
            if status not in ("executed", "canceled"):
                continue                                    # still resting: read it again
            filled = _f(o.get("fill_count_fp"), 0.0) or 0.0
            if filled > 1e-9 and str(o.get("side")) == "yes" and str(o.get("action")) == "buy":
                self.stop_file(f"WRONG DIRECTION: order {t.get('order_id')} bought {filled} YES on {t['mt']}")
                raise SystemExit(5)
            fees = [_f(o.get(k)) for k in ("maker_fees_dollars", "taker_fees_dollars")]
            fee = sum(x for x in fees if x is not None) if any(x is not None for x in fees) else None
            self._book_fill(t, filled, status, fee, o)

    def _book_fill(self, t: dict, filled: float, why: str, fee, o=None) -> None:
        t["fill_checked"] = t["px_checked"] = True
        t.setdefault("contracts_quoted", t["contracts"])
        if filled <= 1e-9:
            t["status"], t["contracts"], t["fee_est"] = "unfilled", 0.0, 0.0
            self.w.write({"k": "unfilled", "t": self.clock(), "mt": t["mt"], "order_id": t.get("order_id"),
                          "why": why, "quoted": t["contracts_quoted"]})
            log(f"! {t['mt']}: NOT filled ({why}): Kalshi has no fill, the ticket is dropped")
        else:
            t["contracts"] = filled
            # Kalshi's price, not our quote's (11/06: 2 of 10 orders filled as the TAKER
            # against a resting order, at a better NO price than quoted, taker fee): our
            # YES-equivalent price = 1 - what the NO cost a contract
            o = o or {}
            mcost, tcost = (_f(o.get("maker_fill_cost_dollars"), 0.0) or 0.0,
                            _f(o.get("taker_fill_cost_dollars"), 0.0) or 0.0)
            if mcost + tcost > 0:
                px = round(1.0 - (mcost + tcost) / filled, 6)
                if abs(px - t["ask"]) > 1e-6:
                    t.setdefault("ask_quoted", t["ask"])
                    t["ask"] = px
                t["taker_fill"] = tcost > 0
            if fee is None:                                 # no fee on the order: the table's
                fee = 0.0 if self.cfg.fee_free(t["legs"]) else maker_fee(filled, t["ask"])
            t["fee_est"] = fee
            if t.get("status") != "executed":           # filled without a quote_executed message
                t["status"], t["executed_at"] = "executed", self.clock()
            self.w.write({"k": "filled", "t": self.clock(), "mt": t["mt"], "order_id": t.get("order_id"),
                          "contracts": filled, "quoted": t["contracts_quoted"], "fee": fee, "ask": t["ask"],
                          "ask_quoted": t.get("ask_quoted"), "taker": t.get("taker_fill")})
            log(f"fill check {t['mt']}: {filled:,.2f} of {t['contracts_quoted']:,.2f} filled, fee ${fee:,.4f}")
        self.book.save()

    def check_direction(self, t: dict) -> None:
        """We should hold NO on the combo (short its YES). A YES position means
        the quote's side was read backwards: write STOP and stop at once. No
        position yet is retried (DIRECTION_TRIES reads, a few seconds apart)."""
        t["dir_tries"] = t.get("dir_tries", 0) + 1
        try:
            js = self.c.get("/portfolio/positions", {"ticker": t["mt"], "limit": 10})
        except Exception as e:
            self._error("position read", e)
            return
        pos = 0.0
        for p in js.get("market_positions") or []:
            if p.get("ticker") == t["mt"]:
                pos = _f(p.get("position_fp"), _f(p.get("position"), 0.0)) or 0.0
        self.w.write({"k": "position", "mt": t["mt"], "position": pos, "try": t["dir_tries"]})
        if pos > 1e-9:
            self.stop_file(f"WRONG DIRECTION: holding {pos} YES on {t['mt']}")
            raise SystemExit(5)
        if pos < -1e-9:
            t["dir_ok"] = True
            self.direction_checks += 1
            log(f"position check {t['mt']}: {pos:+,.1f} = NO, as intended")
        elif t["dir_tries"] >= DIRECTION_TRIES:
            t["dir_ok"] = False
            log(f"! position check {t['mt']}: still none after {t['dir_tries']} reads")
        self.book.save()

    def _error(self, what, e):
        body = str(getattr(e, "body", "") or e)[:300]
        if "rfq_closed" in body.lower():
            self._why("rfq_closed")       # the request closed before our quote landed: no fault, no pause
            return
        low = body.lower()
        with self.lock:
            self.errors += 1
            self.st["errors"] += 1
            if "insufficient" in low:
                self.pause_until = self.clock() + 300
            elif self.errors % 10 == 0:
                self.pause_until = self.clock() + 60
        self.w.write({"k": "err", "what": what, "err": f"{type(e).__name__}: {body}"})
        log(f"! {what}: {type(e).__name__}: {body[:160]}")
        if "insufficient" in low:
            log("! the combo shard is short of cash: quoting paused 5 min")

    # ---- upkeep -----------------------------------------------------------------

    def upkeep(self) -> None:
        now = self.clock()
        with self.lock:
            for qid in [k for k, q in self.quotes.items() if now - q["at"] > QUOTE_TTL_S]:
                del self.quotes[qid]
        if self.cfg.live:
            self.check_fills(now)
        if self.direction_checks < DIRECTION_CHECKS:      # the first fills' side, read back
            t = next((x for x in self.book.tickets if x.get("status") == "executed" and x.get("fill_checked")
                      and "dir_ok" not in x and now - x.get("executed_at", now) >= 3.0), None)
            if t is not None:
                self.check_direction(t)
        if self.cfg.live and now - self.last_settle >= SETTLE_EVERY_S:
            self.last_settle = now
            self.settle()
        if now - self.st_since >= STAT_SECS:
            with self.lock:                             # the senders count into self.st too
                st = self.st
                self._reset_stats()
            rec = dict(st, k="stat", t=now, open=len(self.book.open()),
                       open_loss=round(self.book.open_loss(), 2), live=self.cfg.live)
            self.w.write(rec)
            top = sorted(st["why"].items(), key=lambda kv: -kv[1])[:4]
            log(f"min: rfq {st['rfq']} pre-ok {st['pre_ok']} priced {st['priced']} "
                + (f"quoted {st['quoted']} accepted {st['accepted']} confirmed "
                   f"{st['confirmed']} declined {st['declined']}" if self.cfg.live
                   else f"would quote {st['dry']}")
                + f" | open {len(self.book.open())} can lose ${self.book.open_loss():,.2f} | top skips "
                + ", ".join(f"{k} {v}" for k, v in top))

    def settle(self) -> None:
        open_t = [t for t in self.book.tickets if t.get("status") == "executed" and t.get("fill_checked")]
        if not open_t:
            return
        try:
            js = self.c.get("/markets", {"tickers": ",".join(sorted({t["mt"] for t in open_t})), "limit": 1000})
        except Exception as e:
            self._error("settle read", e)
            return
        res = {m["ticker"]: m for m in js.get("markets") or [] if m.get("ticker")}
        for t in open_t:
            m = res.get(t["mt"]) or {}
            r = str(m.get("result") or "").lower()
            if r not in ("yes", "no"):
                continue
            payoff = 1.0 if r == "yes" else 0.0
            t["status"], t["result"] = "settled", r
            t["pnl"] = t["contracts"] * (t["ask"] - payoff) - t["fee_est"]
            self.w.write({"k": "settled", "mt": t["mt"], "result": r, "pnl": round(t["pnl"], 2)})
            log(f"settled {t['mt']}: {r}, P&L {t['pnl']:+,.2f}")
        self.book.save()


def run(cfg: Config, minutes: float, out_dir: str = OUT_DIR, url: str = WS_URL) -> int:
    os.makedirs(out_dir, exist_ok=True)
    stop_path = os.path.join(out_dir, STOP_NAME)
    if os.path.exists(stop_path):
        log(f"stop file present ({stop_path}); not starting")
        return 3
    lock = lock_file(out_dir)
    if lock is None:
        log("another combo maker is running (lock held); exiting")
        return 4
    import kalshi_subaccount as ks
    # pooled TLS: ~30 ms a read instead of ~0.8 s (the 11/05 dry run fell 74 s behind without it)
    os.environ.setdefault("KALSHI_HTTP_KEEPALIVE", "1")
    client = ks.client()
    client.rate_limit = MinGap(READ_GAP_S)      # the loop's reads / confirms: >= 50 ms apart, no flat 100 ms sleep

    def sender_client():
        c = ks.client()
        c.rate_limit = lambda: None             # senders: paced by the quote cap and their count
        return c
    if cfg.live:
        cash = ks.shard_balances(client).get(1, 0.0)
        log(f"LIVE: combo shard (1) cash ${cash:,.2f}")
        if cash < 1.0:
            log("the combo shard holds no cash: fund it first (python kalshi_subaccount.py shard 300 --to 1)")
            lock.close()
            return 2
    writer = DayWriter(out_dir)
    book = Book(os.path.join(out_dir, STATE_NAME))
    mk = Maker(client, cfg, writer, book, out_dir, client_factory=sender_client)
    writer.write({"k": "start", "t": time.time(), "live": cfg.live,
                  "cfg": {k: sorted(v) if isinstance(v, set) else v for k, v in vars(cfg).items()}})
    log(("LIVE" if cfg.live else "DRY (nothing is sent)") + (", NFL ONLY (fee-free)" if cfg.nfl_only else "")
        + (f", games {cfg.games_mode} of {','.join(sorted(cfg.games))}" if cfg.games else "")
        + ": markup " + "/".join(f"{cfg.markup_for(n) * 100:.3g}" for n in range(cfg.min_legs, cfg.max_legs + 1))
        + f"% at {cfg.min_legs}-{cfg.max_legs} legs (confirm >= half)"
        + (f", all-NFL {cfg.nfl_markup_cut * 100:.3g} pts less" if cfg.nfl_markup_cut else "")
        + f", lead {cfg.min_lead_min:g} min, "
        + (f"legs settle within {cfg.max_days_out:g} days, " if cfg.max_days_out else "")
        + f"{cfg.min_legs}-{cfg.max_legs} main-line legs, fair {cfg.min_fair:.0%}-{cfg.max_fair:.0%}, "
        + (f"loss <= ${cfg.max_ticket_loss:,.0f}/ticket, " if cfg.max_ticket_loss is not None else "")
        + f"payout <= ${cfg.max_payout:,.0f}/ticket, ${cfg.max_game_payout:,.0f}/game, open loss <= "
        f"${cfg.max_open_loss:,.0f}, {cfg.max_tickets} tickets/day, {cfg.quote_workers} quote senders; "
        f"open now {len(book.open())}")
    deadline = time.time() + minutes * 60 if minutes else None
    key_id = getattr(client, "key_id", None)
    backoff, code = 1.0, 0
    try:
        while True:
            if (deadline and time.time() >= deadline) or os.path.exists(stop_path):
                break
            conn = None
            try:
                ts = str(int(time.time() * 1000))
                hdr = {"KALSHI-ACCESS-KEY": key_id,
                       "KALSHI-ACCESS-SIGNATURE": client.sign_pss_text(ts + "GET" + WS_PATH),
                       "KALSHI-ACCESS-TIMESTAMP": ts}
                conn = kalshi_ws.WSConnection(url, headers=hdr, timeout=10.0)
                conn.connect()
                conn.settimeout(0.5)
                sf = int(cfg.shard_factor)
                params = {"channels": ["communications"]}
                if sf > 1:
                    params.update(shard_factor=sf, shard_key=SHARD_KEY % sf)
                conn.send_json({"id": 1, "cmd": "subscribe", "params": params})
                log(f"connected; subscribed communications (1/{sf} of the RFQs)")
                last_up = 0.0
                while True:
                    now = time.time()
                    if now - last_up >= 1.0:
                        last_up = now
                        mk.upkeep()
                        writer.flush()
                        if (deadline and now >= deadline) or os.path.exists(stop_path):
                            raise StopIteration
                        if time.monotonic() - conn.last_rx > STALE_SECS:
                            raise kalshi_ws.WSError("stale feed")
                    try:
                        op, payload = conn.recv_message()
                    except socket.timeout:
                        continue
                    if op != kalshi_ws.OP_TEXT:
                        continue
                    try:
                        msg = json.loads(payload.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    mk.handle(msg)
            except StopIteration:
                break
            except SystemExit as e:
                code = int(e.code or 5)
                break
            except Exception as e:
                log(f"! {type(e).__name__}: {str(e)[:200]}; reconnecting in ~{backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2.0, 60.0)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
    except KeyboardInterrupt:
        pass
    finally:
        mk.close()
        mk.upkeep()
        writer.write({"k": "stop", "t": time.time(), "open": len(book.open()),
                      "open_loss": round(book.open_loss(), 2)})
        writer.close()
        lock.close()
        log(f"stopped; open tickets {len(book.open())}, can lose ${book.open_loss():,.2f}")
    return code


def main(argv=None) -> int:
    d = Config()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true", help="send quotes and confirms (default: DRY)")
    ap.add_argument("--minutes", type=float, default=0.0, help="stop after this long (0 = until stopped)")
    ap.add_argument("--markup", type=float, default=d.markup, help="the markup on a 2-leg parlay")
    ap.add_argument("--markup-per-leg", type=float, default=d.markup_per_leg,
                    help="added to the markup for each leg over 2 (0.01: --markup 0.04 quotes 2 legs at "
                         "4%%, 5 legs at 7%%; 0 = one markup for all)")
    ap.add_argument("--nfl-markup-cut", type=float, default=0.02,
                    help="all-NFL parlays (no maker fee) need this much less edge at the quote and at the "
                         "last look (0.02: 2 legs quoted at 2%% instead of 4%%, confirmed down to 0%% "
                         "instead of 2%%; 0 = no cut). Jack 2026-11-06: \"NFL parlays require 2c less "
                         "edge because there are no maker fees\"")
    ap.add_argument("--max-payout", type=float, default=d.max_payout)
    ap.add_argument("--max-game-payout", type=float, default=d.max_game_payout)
    ap.add_argument("--max-ticket-loss", type=float, default=d.max_ticket_loss,
                    help="the most one ticket can lose, contracts x (1 - our price); raise --max-payout "
                         "above it or the payout cap binds first (default: only --max-payout)")
    ap.add_argument("--max-open-loss", type=float, default=d.max_open_loss)
    ap.add_argument("--max-tickets", type=int, default=d.max_tickets)
    ap.add_argument("--nfl-only", action="store_true",
                    help="only all-NFL combos (no maker fee by Kalshi's table); allows --markup 0.03")
    ap.add_argument("--shard-factor", type=int, default=d.shard_factor,
                    help="see 1 in N of the RFQs (50 = 2%%; 10 = 10%%, ~470 KB/s on the Wi-Fi)")
    ap.add_argument("--games", default="",
                    help="comma list of game keys (e.g. 26NOV05MIAGSW): only parlays with a leg on one")
    ap.add_argument("--games-mode", choices=("any", "all"), default="any",
                    help="all = every leg on those games")
    ap.add_argument("--min-legs", type=int, default=d.min_legs)
    ap.add_argument("--max-legs", type=int, default=d.max_legs)
    ap.add_argument("--max-days-out", type=float, default=d.max_days_out,
                    help="only legs that settle within this many days, so a ticket's cash comes back "
                         "soon (0 = no limit)")
    ap.add_argument("--min-lead-min", type=float, default=d.min_lead_min,
                    help="stop quoting a game this many minutes before its start (Kalshi's start = "
                         "the game market's expiry - 3 h, checked against ESPN)")
    ap.add_argument("--max-quotes-per-min", type=int, default=d.max_quotes_per_min,
                    help="quotes a minute (2 tokens each on the combo shard's write bucket, which the IMM "
                         "does not use)")
    ap.add_argument("--quote-workers", type=int, default=4,
                    help="threads sending quotes, each with its own client, so the loop never waits on a POST "
                         "(0 = inline)")
    ap.add_argument("--sports", default=",".join(DEFAULT_SPORTS),
                    help="sports every leg must be in, from " + ",".join(ALL_SPORTS) + " (tennis and boxing "
                         "are off: their markets do not give start times; 11/06 ITF matches were sold in play)")
    ap.add_argument("--max-leg-move", type=float, default=d.max_leg_move,
                    help="hold a leg out 30 min once its mid moves this much inside 10 min (0 = off)")
    ap.add_argument("--no-extras", action="store_true",
                    help="game / spread / total only: no first-half, quarter, team-total or prop legs")
    ap.add_argument("--min-depth", type=float, default=d.min_depth_usd,
                    help="$ an extra / prop leg needs at BOTH its best bid and best ask")
    ap.add_argument("--max-reads-per-min", type=int, default=d.max_reads_per_min,
                    help="leg reads a minute (10 tokens each); the reads are shared with the IMM's budget")
    ap.add_argument("--allow-thin", action="store_true",
                    help="allow a 3%% markup on fee-paying combos: about breakeven or worse after the "
                         "fee at Kalshi's mids (a mechanics test)")
    a = ap.parse_args(argv)
    floor = 0.03 if (a.nfl_only or a.allow_thin) else 0.05
    if a.markup < floor:
        ap.error(f"markup under {floor:.0%}: " + ("too thin even fee-free" if floor == 0.03 else
                 "it does not cover the 0.035 p(1-p) maker fee with room (all-NFL combos are "
                 "fee-free: --nfl-only allows 3%; --allow-thin allows 3% anyway)"))
    if not 0 <= a.markup_per_leg <= 0.10:
        ap.error("--markup-per-leg 0 to 0.10")
    if not 0 <= a.nfl_markup_cut < a.markup:
        ap.error("--nfl-markup-cut from 0 to under --markup (an all-NFL parlay keeps some markup)")
    if a.min_legs < 2:
        ap.error("--min-legs 2 or more")
    if not a.min_legs <= a.max_legs <= 12:
        ap.error("--max-legs from --min-legs to 12")
    if a.min_lead_min < 2:
        ap.error("--min-lead-min 2 or more: the last minutes before a start are when lines jump")
    games = {g.strip().upper() for g in a.games.split(",") if g.strip()} or None
    cfg = Config(live=a.live, markup=a.markup, markup_per_leg=a.markup_per_leg,
                 nfl_markup_cut=a.nfl_markup_cut, max_payout=a.max_payout,
                 max_game_payout=a.max_game_payout, max_ticket_loss=a.max_ticket_loss,
                 max_open_loss=a.max_open_loss,
                 max_tickets=a.max_tickets, nfl_only=a.nfl_only, shard_factor=a.shard_factor,
                 games=games, games_mode=a.games_mode, min_legs=a.min_legs,
                 max_legs=a.max_legs, min_lead_min=a.min_lead_min,
                 max_reads_per_min=a.max_reads_per_min, max_quotes_per_min=a.max_quotes_per_min,
                 allow_extras=not a.no_extras, min_depth_usd=a.min_depth, max_days_out=a.max_days_out,
                 quote_workers=a.quote_workers, max_leg_move=a.max_leg_move,
                 sports={x.strip().upper() for x in a.sports.split(",") if x.strip()})
    if not cfg.sports or not cfg.sports <= set(ALL_SPORTS):
        ap.error("--sports: a comma list from " + ",".join(ALL_SPORTS))
    if not 0 <= cfg.max_leg_move <= 0.5:
        ap.error("--max-leg-move 0 to 0.5")
    if not 0 <= cfg.quote_workers <= 16:
        ap.error("--quote-workers 0 to 16")
    if cfg.max_ticket_loss is not None and not 10 <= cfg.max_ticket_loss <= 5000:
        ap.error("--max-ticket-loss $10 to $5,000")
    if cfg.max_days_out and not 0.25 <= cfg.max_days_out <= 400:
        ap.error("--max-days-out 0.25 to 400 (0 = no limit)")
    if not 1 <= cfg.max_quotes_per_min <= 600:
        ap.error("--max-quotes-per-min 1-600")
    if cfg.min_depth_usd < 100:
        ap.error("--min-depth $100 or more")
    if not 10 <= cfg.max_reads_per_min <= 600:
        ap.error("--max-reads-per-min 10-600")
    if not 1 <= cfg.shard_factor <= 100:
        ap.error("--shard-factor 1-100")
    return run(cfg, a.minutes)


if __name__ == "__main__":
    sys.exit(main())
