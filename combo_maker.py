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

GUARDS (Jack 2026-11-06, "Yes, build both"): a DAY-LOSS STOP (no new tickets for
the rest of the ET day once the day's P&L -- settled today plus the open tickets
marked from their legs every MARK_EVERY_S -- is at or under -MAX_DAY_LOSS), a SPORT
BURST (3 accepts in one sport inside 30 min, from 2+ requesters: that sport
paused 1 h) and a LEG RUSH (one leg in accepts from 2+ requesters inside 30 min:
its game held out 2 h). The accept that trips one is declined; they persist in
guards.json.

Logs: run-logs\combo-maker\combo_<UTC date>.jsonl (quote / accept / confirm /
decline / executed / filled / unfilled / position / settled / guard / stat /
err), the console, and the LEG TAPE legs_<UTC date>.jsonl: every leg we read,
its book and outside fairs when they change (combo_scorecard.py's prices).

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
import re
import socket
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import kalshi_ws  # noqa: E402
from combo_rfq_logger import WS_PATH, WS_URL, DayWriter, iso_ms, legs_of  # noqa: E402
from combo_rfq_score import (DEFAULT_GRID, MAIN_SERIES, MAKER_FEE_RATE, grid_up, league_of,  # noqa: E402
                             maker_fee, sport_group)
import combo_book_lines as cbl  # noqa: E402
import combo_poly_lines as cpl  # noqa: E402

OUT_DIR = os.environ.get("COMBO_MAKER_DIR", os.path.join(HERE, "run-logs", "combo-maker"))
STATE_NAME = "maker_state.json"
REQUESTERS_NAME = "requesters.json"     # the requester guard's counts and blocks
GUARDS_NAME = "guards.json"             # the day-loss stop, sport pauses, game holds, recent accepts
MARK_EVERY_S = 60.0                     # the open tickets are marked (one read of their legs) this often
LEG_LOG_S = 60.0                        # the leg tape is written this often: a leg read since its last
LEG_REFRESH_S = 300.0                   # line goes again when it changed, or this long after that line
LEG_FORGET_S = 6 * 3600.0               # a leg unread this long leaves the tape's memory
ODDS_DIR = os.environ.get("COMBO_RFQ_DIR", os.path.join(HERE, "run-logs", "combo-rfq"))   # the logger's odds_*.jsonl
LINES_EVERY_S = 60.0                   # the sportsbook lines are re-read this often
POLY_EVERY_S = 180.0                   # Polymarket's moneylines, re-read on their own thread this often
REQ_PRUNE_EVERY_S = 600.0              # requesters with no accept, unseen a day, are forgotten this often
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
GAME_RETRY_S = 600.0                # a game whose winner market a read did not find is not asked again sooner
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
        # LAST-LOOK FLOOR, AFTER FEES (Jack 2026-11-06: "i want last look to have at
        # least 1% edge", then "i mean 1% edge, after fees"): the last look confirms
        # only if our price less the maker fee (0.035 p(1-p) a contract; none on an
        # all-NFL parlay) is at least this much over the fair now, on top of the
        # half-markup bar. A quote whose markup leaves less goes out at the lowest
        # grid price that clears it (2 legs at 5-15c fairs: one tick up), or every
        # accept on it would be declined. 0 here, so a bare Config confirms as before
        # (the tests); the CLI's default is 0.01
        self.confirm_edge_floor = 0.0
        # ...A STEP A LEG (Jack 2026-11-06: "1% would be the floor for 2 legs. and then
        # step it up .5% each leg so 1.5% 3 leg, 2% 4 leg, 2.5% 5 leg, etc"): the floor
        # after the fee grows this much for each leg over 2, half the markup's step
        # (8 legs: 4%). 0 here; the CLI's default is 0.005
        self.confirm_floor_per_leg = 0.0
        # LONG SHOTS (Jack 2026-11-06: "longer shot should charge more edge right?",
        # then "Yes, add it", "and +0.5% when fair is under 20"): (fair below, extra
        # markup) steps on top of the leg ladder -- +0.5 point under 20c, +1 under 10c,
        # +2 under 5c. On the market's prints since the
        # 11/05 evening, our wins under 5c tied up two-thirds of the cash for under a
        # quarter of the expected profit (0.12% per $ tied vs 1.36% at 20-40c), and
        # the other makers already charge long shots more (median +6.5% at 2-3c vs
        # +1.9% at 20-40c). Quote side only: the last look keeps its bars. () here;
        # the CLI's default is 0.05:0.02,0.10:0.01,0.20:0.005
        self.longshot_markup = ()
        # REQUESTER GUARD (Jack 2026-11-06: "Yes, build it"). 11/06 the in-play tennis
        # losses came from two requesters that accepted 3 of 5 and 2 of 4 of our quotes
        # (-$130); a typical requester accepts ~1 in 9,000. A requester (creator_id) whose
        # accepts reach guard_min_accepts at a rate (accepts / our quotes to them) of
        # guard_min_rate or more is flagged: the accept that trips it is declined at the
        # last look, and we stop quoting them for guard_block_hours. 0 = off (here; the
        # CLI's default is 2). The counts and blocks persist in requesters.json
        self.guard_min_accepts = 0
        self.guard_min_rate = 0.10
        self.guard_block_hours = 24.0
        # DAY-LOSS STOP (Jack 2026-11-06, "Yes, build both", after the desk review: there
        # was no P&L stop -- the in-play tennis run sold 6 tickets in 12 min and lost $127
        # before it was stopped by hand). Once the ET day's P&L is at or under
        # -max_day_loss: no new tickets for the rest of the day (RFQs skipped and accepts
        # declined, "day_loss"). The day's P&L = tickets settled today (their P&L, less
        # their mark at the day's first look when sold before today) + the open tickets'
        # marks now, less theirs at the day's first look (0 for a ticket sold today). A
        # mark is contracts x (our price - the parlay's value) - fee, the value the
        # product of the legs' mids (1 / 0 once Kalshi settled a leg), from one read of
        # the open tickets' legs every MARK_EVERY_S. Persists in guards.json; clears at
        # ET midnight; raising the limit over the loss lifts it. None = off (here; the
        # CLI's default is $500)
        self.max_day_loss = None
        # SPORT BURST (same): burst_accepts accepts on parlays with a leg in one sport
        # inside burst_window_min, from 2 or more requesters, pause that sport
        # burst_pause_min; the accept that trips it is declined ("sport_burst", then
        # "sport_paused"). Suddenly the best price in a sport, we likely miss what the
        # other makers know (11/06: tennis in play; our accept rate there was 0.5% of
        # quotes, NBA's 0.002%). The replay of 11/06's 40 accepts: 3 in 30 min tripped
        # only the tennis run, at its 3rd accept (04:25Z), sparing its 5 losing tickets
        # (-$130). One requester's burst is the requester guard's. 0 = off (here; the
        # CLI's default is 3)
        self.burst_accepts = 0
        self.burst_window_min = 30.0
        self.burst_pause_min = 60.0
        # LEG RUSH (same): one leg (ticker and side) in accepts from 2 or more requesters
        # inside rush_window_min holds its game out rush_hold_min, every leg on it: no
        # new quote on the game ("game_held"). Several buyers wanting the same leg off
        # us at once is how a stale leg looks. The 11/06 replay: the tennis legs from
        # 04:25Z, and once Real Madrid (03:04Z, two soccer parlays 16 min apart). It
        # never declines an accept -- the one that trips it, or one on a quote already
        # out -- which goes on to its last look (Jack 2026-11-06: "dont turn down the
        # accept. and stop quoting any parlay from that game for 30min, not 2 hours";
        # it declined the tripping accept and held 120 min before). 0 = off (here; the
        # CLI's default is 30)
        self.rush_window_min = 0.0
        self.rush_hold_min = 30.0
        # ...and only once the worst case on that game -- what its open tickets plus
        # the accept lose if every leg hits -- reaches rush_game_loss (Jack 2026-11-06:
        # "there needs to be an exposure cap to not quote it", then "set worse case
        # threshold to $200 on a game"). 11/06 8:21 PM ET a second requester's Warriors
        # leg held ORL-GSW 2 h with $54.69 riding on the game: a $7.24 6-leg ticket
        # and the declined 66 at 28.1c. Under the bar the game stays quoted, and the
        # outcome and game caps bound it. 0 = any size holds (here; the CLI's default
        # is $200)
        self.rush_game_loss = 0.0
        # OUTCOME CAP (Jack 2026-11-06: "Limit exposure by outcome, not just by game"):
        # the payout that rides on one leg result (ticker and side) across the open
        # tickets, at the quote and at the last look. None = off (here; the CLI's
        # default is $400, under the game cap, so a pile-up on one result binds first)
        self.max_outcome_payout = None
        # SECOND FAIR (Jack 2026-11-06: "A second fair price ... do this if it doesnt slow
        # us down materially"): DraftKings' vig-free moneyline (the RFQ logger writes it
        # to odds_<UTC day>.jsonl every 10 min; combo_book_lines.Lines matches it to a
        # Kalshi game-winner leg). Re-read once a minute in upkeep, matched once a leg, so
        # a quote pays a dict lookup a leg. On a covered leg the side's probability is the
        # higher of Kalshi's mid and the book's -- we sell YES, so the higher fair is the
        # safe one -- and a leg more than max_book_gap from the book skips the parlay
        # (a stale Kalshi leg is what a picker hunts), at the quote and the last look
        self.book_fair = False
        self.max_book_gap = 0.05
        self.odds_dir = None
        # THIRD FAIR (Jack 2026-11-06: "use Polymarket as another fair so DraftKings +
        # Polymarket + Kalshi"): Polymarket's moneylines for UFC and soccer, where the
        # sportsbook feed has none, and the NBA, NFL, NHL and college football, where its
        # line can be missing ("Yes, do both": 11/06 none yet for the 11/07 NBA slate)
        # (combo_poly_lines; read on a background thread every
        # POLY_EVERY_S, matched once a leg). The same rule as the book: the higher of the
        # fairs on a covered leg, a leg over max_book_gap from Kalshi skips ("poly_gap").
        # Only books max_poly_spread wide or less with min_poly_liquidity or more count.
        # 11/06 live check: 122 of 150 soccer / UFC legs covered, within +-1.5 points; with
        # the US leagues, 604 of the 667 game-winner legs out 4 days matched, 500 on books
        # tight enough, none over 3 points from Kalshi's mid (college football 122 of 238,
        # NBA 42 / 68, NHL 42 / 62, NFL 28 / 28, soccer 240 / 243, UFC 26 / 28); the open
        # tickets' legs with an outside price went 41 -> 59 of 77
        self.poly_fair = False
        self.max_poly_spread = 0.03
        self.min_poly_liquidity = 2000.0
        # BOUT GUARD (Jack 2026-11-06, "Yes, do both"): a UFC bout Polymarket shows
        # changed -- one of Kalshi's two fighters booked against someone else (11/06:
        # Kalshi's Bashi vs Brennan, Polymarket's Bashi vs Calderon after Brennan's
        # ACL tear) -- skips both its legs ("bout_changed"), at the quote and the last
        # look: Kalshi settles a bout that doesn't go ahead as listed at a fair price of
        # its own choosing, and whoever asks has the news. Needs poly_fair. 11/06 live
        # check: of 28 UFC legs it flagged the two of that bout, nothing else
        self.bout_guard = True
        # by hand: game keys (26NOV06BASBRE) no parlay may have a leg on ("game_skipped")
        self.skip_games = None
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

    def net_floor_for(self, n_legs: int) -> float:
        """The last look's floor AFTER the fee on an n-leg parlay: confirm_edge_floor
        at 2 legs, + confirm_floor_per_leg for each leg over 2 (1%, 1.5%, 2% ...)."""
        return self.confirm_edge_floor + self.confirm_floor_per_leg * max(0, int(n_legs) - 2)

    def nfl_cut(self, legs) -> float:
        """The all-NFL cut for these legs (0 without legs, or with a non-NFL leg)."""
        return self.nfl_markup_cut if (legs and self.nfl_markup_cut and self.fee_free(legs)) else 0.0

    def longshot_extra(self, fair) -> float:
        """The long-shot step at this fair: the extra of the lowest threshold the
        fair is under (0 at or over them all, or with no fair)."""
        if fair is None:
            return 0.0
        for top, extra in sorted(self.longshot_markup):
            if fair < top - 1e-12:
                return extra
        return 0.0

    def markup_for(self, n_legs: int, legs=None, fair=None) -> float:
        """The markup on an n-leg parlay: `markup` at 2 legs, + `markup_per_leg`
        for each leg over 2, + the long-shot step at `fair`; nfl_markup_cut less
        when every one of `legs` is NFL."""
        m = self.markup + self.markup_per_leg * max(0, int(n_legs) - 2) + self.longshot_extra(fair)
        return max(0.0, m - self.nfl_cut(legs))

    def confirm_edge_for(self, n_legs: int, legs=None) -> float:
        """The last look's minimum edge on an n-leg parlay: confirm_min_edge at 2
        legs, + half the step for each leg over 2 (half the markup, as before);
        the whole all-NFL cut less when every one of `legs` is NFL (floor 0). The
        floor after the fee is net_edge's, at the last look."""
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


def et_next_midnight(ts: float) -> float:
    """The next ET midnight after ts (when the day-loss stop clears)."""
    try:
        import pytz
        tz = pytz.timezone("US/Eastern")
        d = datetime.fromtimestamp(ts, tz)
        return tz.localize(datetime(d.year, d.month, d.day) + timedelta(days=1)).timestamp()
    except ImportError:
        return (math.floor((ts - 5 * 3600) / 86400) + 1) * 86400 + 5 * 3600


MARK_WIDE = 0.15                        # a book wider than this marks at its last trade, if it has one


def leg_value(m: dict, side: str):
    """A leg's chance of going the parlay's way now: 1 / 0 once Kalshi settled it,
    else its mid (1 - mid for a NO leg) -- the last trade instead without a real
    book or with one over MARK_WIDE wide (in play, a 1c / 99c book's 50c says
    nothing); None when there is neither."""
    m = m or {}
    r = str(m.get("result") or "").lower()
    if r in ("yes", "no"):
        return 1.0 if r == side else 0.0
    last = _f(m.get("last_price_dollars"))
    if last is None and _f(m.get("last_price")) is not None:
        last = _f(m.get("last_price")) / 100.0
    if last is not None and not 0.0 <= last <= 1.0:
        last = None
    bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    book = bid is not None and ask is not None and 0.0 <= bid < ask <= 1.0 and not (bid <= 0.0 and ask >= 1.0)
    if book and (ask - bid <= MARK_WIDE + 1e-9 or last is None):
        mid = (bid + ask) / 2.0
    elif last is not None:
        mid = last
    else:
        return None
    return mid if side == "yes" else 1.0 - mid


def parlay_value(legs, markets: dict):
    """The parlay's value now: the product of its legs' leg_value -- 0 once any leg
    lost, None while a live leg can't be valued."""
    v, unknown = 1.0, False
    for tk, side in ((l[0], l[1]) for l in legs):
        x = leg_value(markets.get(tk), side)
        if x is None:
            unknown = True
        elif x <= 0.0:
            return 0.0
        else:
            v *= x
    return None if unknown else v


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
    if cfg.skip_games and any(g in cfg.skip_games for g in games):
        return "game_skipped"
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
            if not is_winner(tk) and (game_start or {}).get(game_of(tk)) is None:
                return "no_game_start", None            # a spread / total: its game's start, or no quote
            start = leg_start(tk, exp, game_start)
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


WINNER_ENDS = ("GAME", "FIGHT", "MATCH", "BOUT")
_LINE_SUFFIX = re.compile(r"^(KX[A-Z0-9]+?)(?:1H|2H|1Q|2Q|3Q|4Q)?(?:TEAMTOTAL|TOTAL|SPREAD|BTTS)$")


def is_winner(ticker: str) -> bool:
    """A game's winner market (KXNBAGAME, KXEPLGAME, KXUFCFIGHT, KXATPMATCH ...): the one
    whose expiry times the game. A spread / total can expire later -- 11/06 every soccer
    league's +1 h, 9 of 113 college basketball lines +3 h, a few college football
    lines +4.5 / +6.5 h -- so its own "expiry - 3 h" can put the start after kickoff
    (11/06 the maker quoted Friday's soccer totals 13-45 min into the matches)."""
    return str(ticker).split("-")[0].endswith(WINNER_ENDS)


def winner_event(ticker: str):
    """The event of a leg's game-winner market: KXEPLTOTAL-26NOV06EVECOV-2 ->
    KXEPLGAME-26NOV06EVECOV, KXNCAAF1HSPREAD-.. -> KXNCAAFGAME-.., a prop by its
    league (KXNBAPTS -> KXNBAGAME); None for a winner market or a series it can't place."""
    parts = str(ticker).split("-")
    if len(parts) < 2 or is_winner(ticker):
        return None
    m = _LINE_SUFFIX.match(parts[0])
    if m:
        return f"{m.group(1)}GAME-{parts[1]}"
    lg = league_of(parts[0])
    return f"KX{lg}GAME-{parts[1]}" if lg else None


def leg_start(ticker: str, exp_s, game_start=None):
    """A main-line leg's start: its own expiry - the league's hours, and a spread /
    total no later than its game's winner market (game_start: game key -> start).
    None when neither is known."""
    own = None if exp_s is None else start_of(ticker, exp_s)
    gs = None if is_winner(ticker) else (game_start or {}).get(game_of(ticker))
    if gs is None:
        return own
    return gs if own is None else min(own, gs)


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
    ask = grid_up(fair * (1.0 + cfg.markup_for(n_legs, legs, fair)), grid or DEFAULT_GRID)
    return ask, round(1.0 - ask, 4)


def fee_per_contract(ask: float, legs, cfg: Config) -> float:
    """The maker fee one contract at our price pays: 0.035 p(1-p) (Kalshi bills it
    to $0.0001), none on an all-NFL parlay."""
    return 0.0 if cfg.fee_free(legs) else maker_fee(1.0, ask)


def net_edge(ask: float, fair: float, legs, cfg: Config) -> float:
    """Our edge after the maker fee, over the fair: (price - fee) / fair - 1."""
    return (ask - fee_per_contract(ask, legs, cfg)) / fair - 1.0 if fair > 0 else 0.0


def net_floor_price(fair: float, legs, cfg: Config, grid=None) -> float:
    """The lowest grid price that keeps the n-leg floor (cfg.net_floor_for) over the
    fair after the fee: p - r p (1 - p) >= fair x (1 + floor), r the maker fee rate."""
    floor = cfg.net_floor_for(len(legs))
    t = fair * (1.0 + floor)
    if cfg.fee_free(legs):
        p = t
    else:
        r = MAKER_FEE_RATE
        p = (-(1.0 - r) + math.sqrt((1.0 - r) ** 2 + 4.0 * r * t)) / (2.0 * r)
    p = grid_up(p, grid or DEFAULT_GRID)
    for _ in range(5):                                  # float dust at a grid boundary: a step up
        if net_edge(p, fair, legs, cfg) >= floor - 1e-9:
            break
        p = grid_up(p + 1e-7, grid or DEFAULT_GRID)
    return p


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

    def outcome_payout(self, ticker: str, side: str) -> float:
        """The payout riding on one leg result: open tickets holding that leg."""
        return sum(t["contracts"] for t in self.open() if any(l[0] == ticker and l[1] == side for l in t["legs"]))

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


def caps_check(book: Book, games: list, contracts: float, ask: float, cfg: Config, day: str, legs=None):
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
    if cfg.max_outcome_payout is not None:
        for l in legs or []:
            if book.outcome_payout(l[0], l[1]) + contracts > cfg.max_outcome_payout + 1e-9:
                return "outcome_cap"
    if book.open_loss() + contracts * (1.0 - ask) > cfg.max_open_loss + 1e-9:
        return "loss_cap"
    if book.day_count(day) >= cfg.max_tickets:
        return "ticket_cap"
    return None


def confirm_check(q: dict, markets: dict, now_s: float, cfg: Config, book: Book, contracts: float,
                  game_start=None, adjust=None):
    """The last look: (why not, fair now). `adjust(legs, markets, now, fair)` ->
    (why, fair): the second fair (Maker.book_adjust)."""
    why, fair = price_check(q["legs"], markets, now_s, cfg, game_start)
    if why:
        return why, fair
    if adjust is not None:
        why, fair = adjust(q["legs"], markets, now_s, fair)
        if why:
            return why, fair
    if q["ask"] < fair * (1.0 + cfg.confirm_edge_for(len(q["legs"]), q["legs"])) - 1e-9:
        return "edge_gone", fair
    floor = cfg.net_floor_for(len(q["legs"]))
    if floor > 0 and net_edge(q["ask"], fair, q["legs"], cfg) < floor - 1e-9:
        return "net_edge", fair                         # under the n-leg floor over the fair after the fee
    return caps_check(book, q["games"], contracts, q["ask"], cfg, et_day(now_s), q["legs"]), fair


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
                 client_factory=None, leg_writer=None):
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
        self.game_start = {}            # game key -> start (from its WINNER market's expiry - 3 h)
        self.game_tried = {}            # game key -> when its winner market was last looked up
        self.mids = {}                  # leg ticker -> deque of (read at, mid): the in-play guard
        self.moving_until = {}          # leg ticker -> held out until
        self.st = {}
        self.direction_checks = 0
        self.pause_until = 0.0
        self.errors = 0
        self.last_settle = 0.0
        # the requester guard: creator id -> {"q": our quotes, "a": accepts, "seen": time}; -> blocked until
        self.req, self.blocked = {}, {}
        self.req_path = os.path.join(out_dir, REQUESTERS_NAME) if out_dir else None
        self.req_pruned = 0.0
        self._load_requesters()
        # the second fair: the sportsbook lines (re-read in upkeep) and each leg's match to them
        self.lines, self.lines_at, self.line_match = None, 0.0, {}
        self.poly, self.poly_match, self.poly_seen = None, {}, None     # PolyLines (run() starts it)
        self.bout_seen, self.bout_logged = {}, set()                    # the bout guard's answers a leg
        # the guards (day-loss stop, sport burst, leg rush), kept in guards.json
        self.guards_path = os.path.join(out_dir, GUARDS_NAME) if out_dir else None
        self.acc_hist = deque()                  # recent accepts: (time, requester, sports, legs)
        self.sport_pause, self.game_hold = {}, {}   # sport / game key -> held out until
        self.halt = None                         # the day-loss stop: {"day", "at", "pnl"}
        self.day_pnl, self.last_mark = None, 0.0
        self._load_guards()
        # the leg tape: leg -> {"b", "a" (its book), "d", "p" (DraftKings / Polymarket YES),
        # "seen" (read at), "at" (last line), "dirty", "xw" (its expiry written)}
        self.leg_w, self.tape, self.tape_at = leg_writer, {}, 0.0
        self._reset_stats()

    def _reset_stats(self):
        with self.lock:
            self.st = {"rfq": 0, "pre_ok": 0, "priced": 0, "quoted": 0, "dry": 0, "accepted": 0,
                       "confirmed": 0, "declined": 0, "executed": 0, "errors": 0, "raised": 0, "why": {}}
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
                        if is_winner(m["ticker"]) and sport_group(m["ticker"].split("-")[0]):
                            self.game_start[game_of(m["ticker"])] = start_of(m["ticker"], ex)
                    self._track_mid(m, now)
                    self._tape(m["ticker"], m, now)
        return {t: self.cache[t][1] for t in tickers if t in self.cache}

    def _tape(self, tk: str, m: dict, now: float) -> None:
        """Note a leg's book for the leg tape (written in upkeep)."""
        if self.leg_w is None:
            return
        bid, ask = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
        e = self.tape.get(tk)
        if e is None:
            e = self.tape[tk] = {"b": None, "a": None, "d": None, "p": None, "at": None, "dirty": True}
        if (bid, ask) != (e["b"], e["a"]):
            e["b"], e["a"], e["dirty"] = bid, ask, True
        e["seen"] = now

    def _tape_src(self, tk: str, book_yes, poly_yes) -> None:
        """Note a leg's outside fairs (YES) for the leg tape."""
        e = self.tape.get(tk) if self.leg_w is not None else None
        if e is None:
            return
        d = None if book_yes is None else round(book_yes, 4)
        p = None if poly_yes is None else round(poly_yes, 4)
        if (d, p) != (e["d"], e["p"]):
            e["d"], e["p"], e["dirty"] = d, p, True

    def write_tape(self, now: float) -> None:
        """One "legs" line: each leg read since its last line whose book or outside
        fairs changed, or LEG_REFRESH_S after that line -- [bid, ask, DraftKings YES,
        Polymarket YES], + its expected expiration (epoch s) the first time."""
        out = {}
        for tk in list(self.tape):
            e = self.tape[tk]
            seen, at = e.get("seen", 0.0), e["at"]
            if now - seen > LEG_FORGET_S:
                del self.tape[tk]
                continue
            if at is not None and (seen <= at or (not e["dirty"] and now - at < LEG_REFRESH_S)):
                continue
            row = [e["b"], e["a"], e["d"], e["p"]]
            if not e.get("xw") and self.exp.get(tk) is not None:
                row.append(round(self.exp[tk]))
                e["xw"] = True
            out[tk] = row
            e["at"], e["dirty"] = now, False
        if out:
            self.leg_w.write({"k": "legs", "t": round(now, 1), "m": out})

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
        """A prop / spread / total leg's game start, from its game's winner market
        (one read a game, kept for the run; a game whose read found none is asked
        again after GAME_RETRY_S)."""
        for tk in {l[0] for l in legs if is_extra(l[0]) or not is_winner(l[0])}:
            game = game_of(tk)
            if game in self.game_start or now - self.game_tried.get(game, -1e18) < GAME_RETRY_S:
                continue
            ev = winner_event(tk)
            if not ev:
                continue
            self.game_tried[game] = now
            self.reads.append(now)
            js = self.c.get("/markets", {"event_ticker": ev, "limit": 10})
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
        cr = str(msg.get("creator_id") or "")[:8]
        if cr and self.blocked.get(cr, 0.0) > now:
            self._why("requester_blocked")
            return
        why = self._guard_block(legs, now)
        if why:
            self._why(why)
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
                st = leg_start(leg[0], self.exp.get(leg[0]), self.game_start)
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
        fair_k = fair
        if not why and (self.cfg.book_fair or self.cfg.poly_fair):
            why, fair = self.book_adjust(legs, mk, now, fair)
        if why:
            self._why(why)
            return
        self.st["priced"] += 1
        ask, no_bid = quote_price(fair, self.cfg, n_legs=len(legs), legs=legs)
        raised = None
        if self.cfg.net_floor_for(len(legs)) > 0:       # the markup leaves under the floor after the fee:
            floor_px = net_floor_price(fair, legs, self.cfg)    # up to the lowest price that clears it
            if floor_px > ask + 1e-9:
                raised, ask, no_bid = ask, floor_px, round(1.0 - floor_px, 4)
        cts = contracts_of(rfq, ask)
        games = [game_of(l[0]) for l in legs]
        why = caps_check(self.book, games, cts, ask, self.cfg, et_day(now), legs)
        if why:
            self._why(why)
            return
        if fair != fair_k:
            rfq = dict(rfq, fair_k=fair_k)              # the Kalshi-only fair, when an outside one raised it
        q = dict(rfq, fair=fair, ask=ask, no_bid=no_bid, mu=round(self.cfg.markup_for(len(legs), legs, fair), 4),
                 contracts=cts, games=games, at=now,
                 lag_ms=None if rfq["rfq_ms"] is None else now * 1000 - rfq["rfq_ms"], read_ms=read_ms)
        if raised is not None:
            q["raised_from"] = raised
            self.st["raised"] += 1
        if not self.cfg.live:
            self.st["dry"] += 1
            self.sent.append(now)                       # DRY keeps LIVE's quote rate
            self.w.write(dict(q, k="would_quote"))
            return
        body = {"rfq_id": rfq["id"], "yes_bid": "0.0000", "no_bid": f"{no_bid:.4f}", "rest_remainder": False}  # "0" is refused: "invalid dollar precision"
        self.sent.append(now)                           # counts against the cap from now, out or not
        self._count_quote(rfq["cr"], now)
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
        try:
            tripped = self._note_accept(q, now, cts)
        except Exception as e:                          # a guard's fault must not lose the last look
            self._error("accept guards", e)
            tripped = None
        if self._requester_accept(q.get("cr") or "", now):
            self._decline(q, "requester_flagged", None, cts)
            return
        why = tripped or self._guard_block(q["legs"], now)
        if why == "game_held":
            why = None              # a held game stops new quotes only, never an accept (LEG RUSH)
        if why:
            self._decline(q, why, None, cts)
            return
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
        why, fair_now = confirm_check(q, mk, now, self.cfg, self.book, cts, self.game_start,
                                      self.book_adjust if (self.cfg.book_fair or self.cfg.poly_fair) else None)
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
             "status": "confirmed", "day": et_day(now), "at": now, "cr": q.get("cr"),
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
        if self.cfg.book_fair and now - self.lines_at >= LINES_EVERY_S:
            self.reload_lines(now)
        if self.cfg.live and now - self.last_mark >= MARK_EVERY_S:
            self.last_mark = now
            try:
                self.mark_book(now)
            except Exception as e:                      # the marks must never stop the loop
                self._error("marks", e)
        if self.leg_w is not None and now - self.tape_at >= LEG_LOG_S:
            self.tape_at = now
            try:
                self.write_tape(now)
            except Exception as e:                      # nor the tape
                self._error("leg tape", e)
        if now - self.req_pruned >= REQ_PRUNE_EVERY_S:
            self.req_pruned = now
            with self.lock:
                for cr in [k for k, r in self.req.items() if not r["a"] and now - r["seen"] > 86400]:
                    del self.req[cr]
                for cr in [k for k, until in self.blocked.items() if until <= now]:
                    del self.blocked[cr]
            for d in (self.sport_pause, self.game_hold):
                for k in [k for k, until in d.items() if until <= now]:
                    del d[k]
            if self.halt is not None and self.halt.get("day") != et_day(now):
                self.halt = None                       # a past day's stop: over
        if now - self.st_since >= STAT_SECS:
            with self.lock:                             # the senders count into self.st too
                st = self.st
                self._reset_stats()
            rec = dict(st, k="stat", t=now, open=len(self.book.open()),
                       open_loss=round(self.book.open_loss(), 2), live=self.cfg.live,
                       day_pnl=None if self.day_pnl is None else round(self.day_pnl, 2),
                       halted=self._halted(now))
            self.w.write(rec)
            top = sorted(st["why"].items(), key=lambda kv: -kv[1])[:4]
            log(f"min: rfq {st['rfq']} pre-ok {st['pre_ok']} priced {st['priced']} "
                + (f"quoted {st['quoted']} accepted {st['accepted']} confirmed "
                   f"{st['confirmed']} declined {st['declined']}" if self.cfg.live
                   else f"would quote {st['dry']}")
                + f" | open {len(self.book.open())} can lose ${self.book.open_loss():,.2f} | top skips "
                + ", ".join(f"{k} {v}" for k, v in top))

    # ---- the guards: day-loss stop, sport burst, leg rush -------------------------

    def _halted(self, now: float) -> bool:
        """The day-loss stop holds: tripped today, at a loss the limit still covers
        (raising --max-day-loss over it lifts it; off lifts it)."""
        h, lim = self.halt, self.cfg.max_day_loss
        return bool(h) and lim is not None and h.get("day") == et_day(now) and _f(h.get("pnl"), 0.0) <= -lim + 1e-9

    def _guard_block(self, legs, now: float):
        """Why the guards keep these legs out now (None = they don't)."""
        if self.halt is not None and self._halted(now):
            return "day_loss"
        if self.sport_pause and any(self.sport_pause.get(sport_of(str(l[0]).split("-")[0]), 0.0) > now
                                    for l in legs):
            return "sport_paused"
        if self.game_hold and any(self.game_hold.get(game_of(l[0]), 0.0) > now for l in legs):
            return "game_held"
        return None

    def _guard_rec(self, what: str, key: str, until: float, detail: str, now: float) -> None:
        self.w.write({"k": "guard", "t": now, "what": what, "key": key, "until": until, "detail": detail})
        log(f"! guard {what}: {key} held out until {datetime.fromtimestamp(until, timezone.utc):%H:%MZ} "
            f"({detail})")

    def game_loss(self, game: str) -> float:
        """The worst case on one game: what its open tickets lose if every leg hits,
        contracts x (1 - ask) each."""
        return sum(t["contracts"] * (1.0 - t["ask"]) for t in self.book.open() if game in t["games"])

    def _note_accept(self, q: dict, now: float, cts=None):
        """Count an accept for the sport burst and the leg rush; "sport_burst" when
        it trips the sport burst (the accept is then declined), else None. A leg rush
        it trips holds the game's new quotes and lets the accept through; it holds
        only once the game's worst case, the accept's own (`cts` contracts, else the
        quote's) included, reaches rush_game_loss."""
        legs = tuple((str(l[0]), str(l[1])) for l in q["legs"])
        sports = tuple(sorted({sport_of(tk.split("-")[0]) for tk, _ in legs}))
        cr = q.get("cr") or ""
        keep = max(self.cfg.burst_window_min if self.cfg.burst_accepts > 0 else 0.0, self.cfg.rush_window_min) * 60
        while self.acc_hist and now - self.acc_hist[0][0] > keep:
            self.acc_hist.popleft()
        hist = list(self.acc_hist)
        if keep > 0:
            self.acc_hist.append((now, cr, sports, legs))
        why = None
        if self.cfg.burst_accepts > 0:
            w = self.cfg.burst_window_min * 60
            for s in sports:
                recent = [c for t, c, ss, _ in hist if s in ss and now - t <= w] + [cr]
                if len(recent) >= self.cfg.burst_accepts and len(set(recent)) >= 2:
                    until = now + self.cfg.burst_pause_min * 60
                    self.sport_pause[s] = max(self.sport_pause.get(s, 0.0), until)
                    self._guard_rec("sport_burst", s, until, f"{len(recent)} accepts in {self.cfg.burst_window_min:g}"
                                    f" min from {len(set(recent))} requesters", now)
                    why = why or "sport_burst"
        if self.cfg.rush_window_min > 0:
            w = self.cfg.rush_window_min * 60
            for leg in legs:
                others = {c for t, c, _, ls in hist if leg in ls and now - t <= w and c != cr}
                if others and self.cfg.rush_game_loss > 0:
                    g = game_of(leg[0])
                    risk = (self.game_loss(g)
                            + (cts if cts is not None else q["contracts"]) * (1.0 - q["ask"]))
                    if risk < self.cfg.rush_game_loss - 1e-9:
                        log(f"guard leg_rush: {leg[0]} {leg[1]} accepted by {len(others) + 1} requesters in "
                            f"{self.cfg.rush_window_min:g} min, but the worst case on {g} is ${risk:,.2f} "
                            f"(under ${self.cfg.rush_game_loss:,.0f}): the game stays quoted")
                        continue
                if others:
                    g = game_of(leg[0])
                    until = now + self.cfg.rush_hold_min * 60
                    self.game_hold[g] = max(self.game_hold.get(g, 0.0), until)
                    self._guard_rec("leg_rush", g, until, f"{leg[0]} {leg[1]} accepted by {len(others) + 1} "
                                    f"requesters in {self.cfg.rush_window_min:g} min", now)
        self._save_guards()
        return why

    def mark_book(self, now: float) -> None:
        """Mark the open tickets from one read of their legs, total the ET day's P&L
        (Config.max_day_loss), trip the day-loss stop. A ticket whose live leg can't be
        valued keeps its last mark; a failed read leaves the day's P&L as it was."""
        day = et_day(now)
        open_t = [t for t in self.book.tickets if t.get("status") == "executed" and t.get("fill_checked")]
        legs = sorted({l[0] for t in open_t for l in t["legs"]})
        mk = {}
        try:
            for i in range(0, len(legs), 100):
                js = self.c.get("/markets", {"tickers": ",".join(legs[i:i + 100]), "limit": 1000})
                mk.update({m["ticker"]: m for m in js.get("markets") or [] if m.get("ticker")})
        except Exception as e:
            self._error("mark read", e)
            return
        # ...onto the leg tape too (2026-11-07, the scorecard's "our fills against the
        # close"): an open ticket's leg nobody else asks about still gets its book on the
        # tape every minute up to its start, so the ticket has a closing line
        for tk, m in mk.items():
            ex = _iso_s(m.get("expected_expiration_time"))
            if ex is not None:
                self.exp[tk] = ex
            self._tape(tk, m, now)
        total = 0.0
        for t in self.book.tickets:
            if t.get("status") == "settled":
                if t.get("settled_day") == day:
                    total += (_f(t.get("pnl"), 0.0) or 0.0) - (t.get("m0", 0.0) if t.get("m0_day") == day else 0.0)
                continue
            if not (t.get("status") == "executed" and t.get("fill_checked")):
                continue
            v = parlay_value(t["legs"], mk)
            if v is not None:
                t["mark"] = round(t["contracts"] * (t["ask"] - v) - (t.get("fee_est") or 0.0), 4)
            if t.get("mark") is None:
                continue
            if t.get("m0_day") != day:                  # the day's first look: its baseline
                t["m0_day"], t["m0"] = day, (t["mark"] if t.get("day") != day else 0.0)
            total += t["mark"] - t["m0"]
        self.day_pnl = total
        if open_t:
            self.book.save()
        lim = self.cfg.max_day_loss
        if lim is not None and total <= -lim + 1e-9 and not self._halted(now):
            self.halt = {"day": day, "at": now, "pnl": round(total, 2)}
            self._guard_rec("day_loss", day, et_next_midnight(now),
                            f"today's P&L ${total:,.2f} (settled + marked) is at or under -${lim:,.0f}", now)
            self._save_guards()

    def _load_guards(self) -> None:
        if not self.guards_path or not os.path.exists(self.guards_path):
            return
        try:
            with open(self.guards_path, encoding="utf-8") as f:
                js = json.load(f)
            self.sport_pause = {k: float(v) for k, v in (js.get("sport_pause") or {}).items()}
            self.game_hold = {k: float(v) for k, v in (js.get("game_hold") or {}).items()}
            self.halt = js.get("halt") or None
            self.acc_hist = deque((float(a[0]), str(a[1]), tuple(a[2]), tuple(tuple(x) for x in a[3]))
                                  for a in js.get("accepts") or [])
        except (OSError, ValueError, TypeError, IndexError) as e:
            log(f"! {GUARDS_NAME} unreadable ({e!r}): starting the guards empty")

    def _save_guards(self) -> None:
        if not self.guards_path:
            return
        js = {"sport_pause": self.sport_pause, "game_hold": self.game_hold, "halt": self.halt,
              "accepts": [[a[0], a[1], list(a[2]), [list(x) for x in a[3]]] for a in self.acc_hist]}
        try:
            tmp = self.guards_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(js, f)
            os.replace(tmp, self.guards_path)
        except OSError as e:
            self._error("guards save", e)

    # ---- the requester guard -----------------------------------------------------

    def _load_requesters(self) -> None:
        if not self.req_path or not os.path.exists(self.req_path):
            return
        try:
            with open(self.req_path, encoding="utf-8") as f:
                js = json.load(f)
            self.req = {k: dict(v) for k, v in (js.get("req") or {}).items()}
            self.blocked = {k: float(v) for k, v in (js.get("blocked") or {}).items()}
        except (OSError, ValueError) as e:
            log(f"! {REQUESTERS_NAME} unreadable ({e!r}): starting the guard empty")

    def _save_requesters(self) -> None:
        """The requesters that ever accepted (and the blocks): the rest are counts
        only, rebuilt as they quote again."""
        if not self.req_path:
            return
        with self.lock:
            js = {"req": {k: v for k, v in self.req.items() if v["a"]}, "blocked": dict(self.blocked)}
        try:
            tmp = self.req_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(js, f)
            os.replace(tmp, self.req_path)
        except OSError as e:
            self._error("requesters save", e)

    def _count_quote(self, cr: str, now: float) -> None:
        if not cr:
            return
        with self.lock:
            r = self.req.get(cr)
            if r is None:
                r = self.req[cr] = {"q": 0, "a": 0, "seen": now}
            r["q"] += 1
            r["seen"] = now

    def _requester_accept(self, cr: str, now: float) -> bool:
        """Count a buyer's accept; True when it flags them (the accept is then declined)."""
        if not cr:
            return False
        with self.lock:
            r = self.req.get(cr)
            if r is None:
                r = self.req[cr] = {"q": 0, "a": 0, "seen": now}
            r["a"] += 1
            r["seen"] = now
            n = max(r["q"], r["a"])
            flag = (self.cfg.guard_min_accepts > 0 and r["a"] >= self.cfg.guard_min_accepts
                    and r["a"] / n >= self.cfg.guard_min_rate - 1e-12)
            if flag:
                self.blocked[cr] = now + self.cfg.guard_block_hours * 3600.0
            quotes, accepts = r["q"], r["a"]
        if flag:
            self.w.write({"k": "requester_block", "t": now, "cr": cr, "quotes": quotes, "accepts": accepts,
                          "until": self.blocked[cr]})
            log(f"! requester {cr} accepted {accepts} of our {quotes} quotes: blocked "
                f"{self.cfg.guard_block_hours:g} h")
        self._save_requesters()
        return flag

    # ---- the second fair -----------------------------------------------------------

    def reload_lines(self, now: float) -> None:
        """The sportsbook lines from the logger's odds files (yesterday's and today's)."""
        self.lines_at = now
        try:
            d = datetime.fromtimestamp(now, timezone.utc).date()
            rows = cbl.load(self.cfg.odds_dir or ODDS_DIR, d, d)
            self.lines, self.line_match = cbl.Lines(rows), {}
        except Exception as e:
            self._error("odds read", e)

    def _book_yes(self, tk: str, m: dict, now: float):
        """The sportsbook's vig-free YES probability for a leg (None: not covered)."""
        lines = self.lines if self.cfg.book_fair else None
        if lines is None:
            return None
        hit = self.line_match.get(tk, False)
        if hit is False:
            hit = self.line_match[tk] = lines.match(tk, m)
        ph = lines.p_home(hit[0], now) if hit else None
        return None if ph is None else (ph[0] if hit[1] else 1.0 - ph[0])

    def _poly_now(self):
        """The Polymarket lines once read (None: off, or not read yet); a new read
        clears the legs' matches and bout answers."""
        poly = self.poly if self.cfg.poly_fair else None
        if poly is None or poly.at is None:
            return None
        if poly.at != self.poly_seen:                   # a new read: match the legs afresh
            self.poly_match, self.bout_seen, self.poly_seen = {}, {}, poly.at
        return poly

    def _poly_yes(self, tk: str, m: dict):
        """Polymarket's YES mid for a leg, from a book max_poly_spread wide or less with
        min_poly_liquidity or more (None: not covered, or too thin to say)."""
        poly = self._poly_now()
        if poly is None:
            return None
        hit = self.poly_match.get(tk, False)
        if hit is False:
            hit = self.poly_match[tk] = poly.match(tk, m)
        pp = poly.p_yes(hit)
        if pp is None or pp[1] > self.cfg.max_poly_spread + 1e-9 or pp[2] < self.cfg.min_poly_liquidity:
            return None
        return pp[0]

    def _bout_changed(self, tk: str, m: dict):
        """The bout guard: the Polymarket fight showing this UFC leg's bout changed, or None."""
        if not self.cfg.bout_guard or not str(tk).startswith("KXUFCFIGHT-"):
            return None
        poly = self._poly_now()
        if poly is None:
            return None
        hit = self.bout_seen.get(tk, False)
        if hit is False:
            hit = self.bout_seen[tk] = poly.bout_changed(tk, m)
            g = game_of(tk)
            if hit and g not in self.bout_logged:
                self.bout_logged.add(g)
                log(f"bout guard: {g} is {' vs '.join(cpl.kalshi_sides(m) or ('?', '?'))} on Kalshi, "
                    f"Polymarket has {hit}: both legs out")
        return hit

    def book_adjust(self, legs, markets: dict, now: float, fair: float):
        """(why, fair) from the outside fairs: on each leg the sportsbook (DraftKings)
        or Polymarket covers, the highest of Kalshi's mid and theirs for the side;
        "book_gap" / "poly_gap" when one is more than max_book_gap from Kalshi's.
        Legs neither covers keep Kalshi's mid. "bout_changed": a UFC leg whose bout
        Polymarket shows changed (the bout guard)."""
        if self.lines is None and self.poly is None:
            return None, fair
        probs, moved = [], False
        for tk, side in ((l[0], l[1]) for l in legs):
            m = markets.get(tk) or {}
            if self._bout_changed(tk, m):
                return "bout_changed", fair
            bid, ask = _f(m.get("yes_bid_dollars"), 0.0), _f(m.get("yes_ask_dollars"), 1.0)
            k_yes = (bid + ask) / 2.0
            p_k = k_yes if side == "yes" else 1.0 - k_yes
            best = p_k
            b_yes, p_yes = self._book_yes(tk, m, now), self._poly_yes(tk, m)
            self._tape_src(tk, b_yes, p_yes)
            for src, o_yes in (("book", b_yes), ("poly", p_yes)):
                if o_yes is None:
                    continue
                p_o = o_yes if side == "yes" else 1.0 - o_yes
                if abs(p_o - p_k) > self.cfg.max_book_gap + 1e-9:
                    return f"{src}_gap", fair
                best = max(best, p_o)
            probs.append(best)
            moved = moved or best > p_k
        return None, (max(fair, math.prod(probs)) if moved else fair)

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
            t["settled_at"] = self.clock()
            t["settled_day"] = et_day(t["settled_at"])
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
    leg_writer = DayWriter(out_dir, prefix="legs")      # the leg tape: combo_scorecard.py's prices
    book = Book(os.path.join(out_dir, STATE_NAME))
    mk = Maker(client, cfg, writer, book, out_dir, client_factory=sender_client, leg_writer=leg_writer)
    if cfg.poly_fair:                                   # the third fair: Polymarket, on its own thread
        mk.poly = cpl.PolyLines(log=lambda s: log(s))
        mk.poly.start(POLY_EVERY_S)
    writer.write({"k": "start", "t": time.time(), "live": cfg.live,
                  "cfg": {k: sorted(v) if isinstance(v, set) else v for k, v in vars(cfg).items()}})
    log(("LIVE" if cfg.live else "DRY (nothing is sent)") + (", NFL ONLY (fee-free)" if cfg.nfl_only else "")
        + (f", games {cfg.games_mode} of {','.join(sorted(cfg.games))}" if cfg.games else "")
        + (f", skipping {','.join(sorted(cfg.skip_games))}" if cfg.skip_games else "")
        + ": markup " + "/".join(f"{cfg.markup_for(n) * 100:.3g}" for n in range(cfg.min_legs, cfg.max_legs + 1))
        + f"% at {cfg.min_legs}-{cfg.max_legs} legs (confirm >= half and >= {cfg.confirm_edge_floor * 100:.3g}% +{cfg.confirm_floor_per_leg * 100:.3g}%/leg after the fee)"
        + (f", all-NFL {cfg.nfl_markup_cut * 100:.3g} pts less" if cfg.nfl_markup_cut else "")
        + ((", long shots " + ", ".join(f"+{e * 100:.3g} pts under {t * 100:.3g}c"
                                        for t, e in sorted(cfg.longshot_markup, reverse=True)))
           if cfg.longshot_markup else "")
        + (f", day-loss stop ${cfg.max_day_loss:,.0f}" if cfg.max_day_loss is not None else ", NO day-loss stop")
        + (f", sport burst {cfg.burst_accepts} accepts/{cfg.burst_window_min:g} min -> {cfg.burst_pause_min:g} min"
           if cfg.burst_accepts else "")
        + (f", leg rush {cfg.rush_window_min:g} min -> game out {cfg.rush_hold_min:g} min"
           + (f" once its worst case reaches ${cfg.rush_game_loss:,.0f}" if cfg.rush_game_loss > 0 else "")
           if cfg.rush_window_min else "")
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
                        leg_writer.flush()
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
        leg_writer.close()
        lock.close()
        log(f"stopped; open tickets {len(book.open())}, can lose ${book.open_loss():,.2f}")
    return code


def parse_longshot(s: str) -> tuple:
    """'0.05:0.02,0.10:0.01' -> ((0.05, 0.02), (0.10, 0.01)): fair-below:extra
    pairs, each fair in (0, 0.40] and each extra in [0, 0.20]; '' -> ()."""
    out = []
    for part in (s or "").split(","):
        if not part.strip():
            continue
        try:
            top, extra = (float(x) for x in part.split(":"))
        except ValueError:
            raise ValueError(f"{part!r} is not fair-below:extra (0.05:0.02)") from None
        if not (0.0 < top <= 0.40 and 0.0 <= extra <= 0.20):
            raise ValueError(f"{part!r}: the fair must be in (0, 0.40] and the extra in [0, 0.20]")
        out.append((top, extra))
    return tuple(sorted(out))


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
    ap.add_argument("--confirm-edge-floor", type=float, default=0.01,
                    help="the last look confirms only with this much edge over the fair AFTER the maker fee "
                         "(0.035 p(1-p); none on all-NFL parlays); a quote whose markup leaves less goes out "
                         "at the lowest price that clears it (0 = off). Jack 2026-11-06: \"i mean 1%% edge, "
                         "after fees\"")
    ap.add_argument("--longshot-markup", default="0.05:0.02,0.10:0.01,0.20:0.005",
                    help="extra markup on long shots, as fair-below:extra pairs (default: +2 pts under 5c, "
                         "+1 under 10c, +0.5 under 20c; '' = off). Quote side only. Jack 2026-11-06: \"longer shot should "
                         "charge more edge right?\"")
    ap.add_argument("--confirm-floor-per-leg", type=float, default=0.005,
                    help="the floor after the fee grows this much for each leg over 2 (0.005: 1%%, 1.5%%, 2%% "
                         "... 4%% at 8 legs). Jack 2026-11-06: \"1%% would be the floor for 2 legs. and then "
                         "step it up .5%% each leg\"")
    ap.add_argument("--max-payout", type=float, default=d.max_payout)
    ap.add_argument("--max-game-payout", type=float, default=d.max_game_payout)
    ap.add_argument("--max-outcome-payout", type=float, default=400.0,
                    help="the payout that may ride on one leg result (ticker and side) across the open "
                         "tickets (0 = off). Jack 2026-11-06: \"Limit exposure by outcome, not just by game\"")
    ap.add_argument("--guard-min-accepts", type=int, default=2,
                    help="the requester guard: this many accepts from one requester at --guard-min-rate or more "
                         "of our quotes to them blocks them --guard-block-hours (the accept that trips it is "
                         "declined; 0 = off)")
    ap.add_argument("--guard-min-rate", type=float, default=d.guard_min_rate)
    ap.add_argument("--guard-block-hours", type=float, default=d.guard_block_hours)
    ap.add_argument("--max-day-loss", type=float, default=500.0,
                    help="no new tickets for the rest of the ET day once the day's P&L (tickets settled today + "
                         "the open ones marked from their legs, each less its mark at the day's first look) is at "
                         "or under minus this (0 = off). Jack 2026-11-06: \"Yes, build both\"")
    ap.add_argument("--burst-accepts", type=int, default=3,
                    help="this many accepts on parlays with a leg in one sport inside --burst-window-min, from 2+ "
                         "requesters, pause that sport --burst-pause-min; the accept that trips it is declined "
                         "(0 = off)")
    ap.add_argument("--burst-window-min", type=float, default=d.burst_window_min)
    ap.add_argument("--burst-pause-min", type=float, default=d.burst_pause_min)
    ap.add_argument("--rush-window-min", type=float, default=30.0,
                    help="one leg in accepts from 2+ requesters inside this many minutes holds its game out "
                         "--rush-hold-min; the accept that trips it is declined (0 = off)")
    ap.add_argument("--rush-hold-min", type=float, default=d.rush_hold_min)
    ap.add_argument("--rush-game-loss", type=float, default=200.0,
                    help="the leg rush holds a game only once its worst case -- what its open tickets "
                         "plus the accept lose if every leg hits -- reaches this many dollars (0 = any size)")
    ap.add_argument("--no-book-fair", action="store_true",
                    help="drop the second fair (DraftKings' vig-free moneyline on game-winner legs, from the "
                         "RFQ logger's odds files)")
    ap.add_argument("--no-poly-fair", action="store_true",
                    help="drop the third fair (Polymarket's moneylines on UFC, soccer, NBA, NFL, NHL and "
                         "college football legs) and the bout guard with it")
    ap.add_argument("--max-poly-spread", type=float, default=d.max_poly_spread,
                    help="a Polymarket book this wide or wider doesn't count (0.03 = 3c)")
    ap.add_argument("--min-poly-liquidity", type=float, default=d.min_poly_liquidity)
    ap.add_argument("--no-bout-guard", action="store_true",
                    help="keep quoting a UFC bout Polymarket shows changed (a fighter booked against "
                         "someone else)")
    ap.add_argument("--skip-games", default="",
                    help="comma list of game keys (26NOV06BASBRE) or tickers on them: no parlay with a leg "
                         "on one")
    ap.add_argument("--max-book-gap", type=float, default=d.max_book_gap,
                    help="skip a parlay with a leg this far from the book's probability (0.05 = 5 points)")
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
    if not 0 <= a.confirm_edge_floor < a.markup - a.nfl_markup_cut - 1e-9:
        ap.error("--confirm-edge-floor from 0 to under the lowest quote markup (--markup minus "
                 "--nfl-markup-cut), or a 2-leg all-NFL quote fails its own last look")
    if not 0 <= a.confirm_floor_per_leg <= a.markup_per_leg + 1e-9:
        ap.error("--confirm-floor-per-leg from 0 to --markup-per-leg (the floor may not outgrow the markup)")
    try:
        longshot = parse_longshot(a.longshot_markup)
    except ValueError as e:
        ap.error(f"--longshot-markup: {e}")
    if a.min_legs < 2:
        ap.error("--min-legs 2 or more")
    if not a.min_legs <= a.max_legs <= 12:
        ap.error("--max-legs from --min-legs to 12")
    if a.min_lead_min < 2:
        ap.error("--min-lead-min 2 or more: the last minutes before a start are when lines jump")
    games = {g.strip().upper() for g in a.games.split(",") if g.strip()} or None
    skip = {game_of(g.strip().upper()) for g in a.skip_games.split(",") if g.strip()} or None
    cfg = Config(live=a.live, markup=a.markup, markup_per_leg=a.markup_per_leg,
                 nfl_markup_cut=a.nfl_markup_cut, confirm_edge_floor=a.confirm_edge_floor,
                 confirm_floor_per_leg=a.confirm_floor_per_leg, longshot_markup=longshot,
                 max_payout=a.max_payout,
                 max_game_payout=a.max_game_payout, max_ticket_loss=a.max_ticket_loss,
                 max_outcome_payout=a.max_outcome_payout or None, guard_min_accepts=a.guard_min_accepts,
                 guard_min_rate=a.guard_min_rate, guard_block_hours=a.guard_block_hours,
                 max_day_loss=a.max_day_loss or None, burst_accepts=a.burst_accepts,
                 burst_window_min=a.burst_window_min, burst_pause_min=a.burst_pause_min,
                 rush_window_min=a.rush_window_min, rush_hold_min=a.rush_hold_min,
                 rush_game_loss=a.rush_game_loss,
                 book_fair=not a.no_book_fair, max_book_gap=a.max_book_gap,
                 poly_fair=not a.no_poly_fair, max_poly_spread=a.max_poly_spread,
                 min_poly_liquidity=a.min_poly_liquidity, bout_guard=not a.no_bout_guard, skip_games=skip,
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
    if cfg.max_outcome_payout is not None and not 10 <= cfg.max_outcome_payout <= 100_000:
        ap.error("--max-outcome-payout $10 to $100,000 (0 = off)")
    if not 0 <= cfg.guard_min_accepts <= 100 or not 0 < cfg.guard_min_rate <= 1 or not 0 < cfg.guard_block_hours <= 720:
        ap.error("--guard-min-accepts 0-100, --guard-min-rate (0, 1], --guard-block-hours (0, 720]")
    if not 0.001 <= cfg.max_poly_spread <= 0.2 or cfg.min_poly_liquidity < 0:
        ap.error("--max-poly-spread 0.001 to 0.2, --min-poly-liquidity 0 or more")
    if not 0.005 <= cfg.max_book_gap <= 0.5:
        ap.error("--max-book-gap 0.005 to 0.5")
    if cfg.max_day_loss is not None and not 10 <= cfg.max_day_loss <= 100_000:
        ap.error("--max-day-loss $10 to $100,000 (0 = off)")
    if not 0 <= cfg.burst_accepts <= 100 or not 0 < cfg.burst_window_min <= 1440 or not 0 < cfg.burst_pause_min <= 1440:
        ap.error("--burst-accepts 0-100, --burst-window-min and --burst-pause-min (0, 1440]")
    if not 0 <= cfg.rush_window_min <= 1440 or not 0 < cfg.rush_hold_min <= 2880:
        ap.error("--rush-window-min 0-1440, --rush-hold-min (0, 2880]")
    if not 0 <= cfg.rush_game_loss <= 100_000:
        ap.error("--rush-game-loss $0 to $100,000 (0 = any size)")
    return run(cfg, a.minutes)


if __name__ == "__main__":
    sys.exit(main())
