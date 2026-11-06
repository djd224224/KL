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
  the payout on any one game, over open tickets      <= MAX_GAME_PAYOUT
  what the open tickets can lose (contracts x NO px) <= MAX_OPEN_LOSS
  confirmed tickets an ET day                        <= MAX_TICKETS
  quotes a minute                                    <= MAX_QUOTES_PER_MIN
The exchange adds its own: only the combo shard's cash (shard 1) can back a
fill, so what Jack moves there is the most the pilot can ever put at risk.

SAFETY: nothing is sent without --live (DRY prices and logs the quotes it
would send). After each fill it reads the position back: a YES position on a
combo (the wrong direction) writes the STOP file and stops. STOP file:
run-logs\combo-maker\combo_maker.stop (it also refuses to start while the
file exists). One instance at a time (combo_maker.lock). Open tickets persist
in maker_state.json, so a restart keeps counting them against the caps;
settlements are read every SETTLE_EVERY_S and free their exposure.

Logs: run-logs\combo-maker\combo_<UTC date>.jsonl (quote / accept / confirm /
decline / executed / position / settled / stat / err) and the console.

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
import time
from collections import deque
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import kalshi_ws  # noqa: E402
from combo_rfq_logger import WS_PATH, WS_URL, DayWriter, iso_ms, legs_of  # noqa: E402
from combo_rfq_score import DEFAULT_GRID, MAIN_SERIES, grid_up, maker_fee  # noqa: E402

OUT_DIR = os.environ.get("COMBO_MAKER_DIR", os.path.join(HERE, "run-logs", "combo-maker"))
STATE_NAME = "maker_state.json"
STOP_NAME = "combo_maker.stop"
LOCK_NAME = "combo_maker.lock"
# MAIN_SERIES (US-league game / spread / total) comes from combo_rfq_score, so
# the shadow's Strategy 1 table and this pilot share one definition
GAME_HOURS = 3.0
SHARD_FACTOR, SHARD_KEY = 50, 1     # a 2% slice of RFQs (the logger reads key 0)
STALE_SECS = 30.0
SETTLE_EVERY_S = 300.0
STAT_SECS = 60.0
QUOTE_TTL_S = 120.0                 # forget our quote this long after it was sent
DIRECTION_CHECKS = 3                # read back the position of the first N fills
DIRECTION_TRIES = 5                 # ...up to this many reads each
ET_OFFSET = None                    # filled by et_today()


class Config:
    def __init__(self, **kw):
        self.live = False
        self.markup = 0.10
        self.confirm_min_edge = None        # None = half the markup (0.05 at the default 10%)
        # NFL ONLY (Jack 2026-11-05, after the shadow: 147 Strategy-1 prints sat a
        # median +1.8% over fair, 3% of them over +5%): every leg an NFL main
        # line, which Kalshi's fee table exempts from the combo maker fee
        # ("Combos (excluding uncorrelated NFL)"), so a 3% markup is all edge
        self.nfl_only = False
        self.shard_factor = SHARD_FACTOR    # see 1/N of the RFQs (50 = 2%, ~90 KB/s; 10 = 10%, ~470 KB/s)
        self.min_legs, self.max_legs = 3, 8
        self.min_fair, self.max_fair = 0.02, 0.40
        self.max_leg_spread = 0.03
        self.min_lead_min = 30.0
        self.max_payout = 150.0
        self.min_premium = 0.50
        self.max_game_payout = 300.0
        self.max_open_loss = 400.0
        self.max_tickets = 30
        self.max_quotes_per_min = 60
        self.max_reads_per_min = 60         # leg reads (10 tokens each): ~10 tokens/s at most
        self.max_rfq_age_ms = 1500.0        # an RFQ older than this when we get to it: skip
        self.leg_cache_s = 15.0
        for k, v in kw.items():
            if not hasattr(self, k):
                raise TypeError(f"unknown setting {k}")
            setattr(self, k, v)
        if self.confirm_min_edge is None:
            self.confirm_min_edge = self.markup / 2.0

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

def pre_check(legs: list, cfg: Config):
    """Why an RFQ's legs are out before any read (None = worth pricing)."""
    if not (cfg.min_legs <= len(legs) <= cfg.max_legs):
        return "legs"
    if any(len(l) < 2 or l[1] not in ("yes", "no") or len(l) > 2 for l in legs):
        return "side"                                   # a scalar leg too
    if any(not MAIN_SERIES.match(str(l[0]).split("-")[0]) for l in legs):
        return "not_main"
    if cfg.nfl_only and not cfg.fee_free(legs):
        return "not_nfl"
    games = [game_of(l[0]) for l in legs]
    if len(set(games)) < len(games):
        return "same_game"
    return None


def price_check(legs: list, markets: dict, now_s: float, cfg: Config):
    """(why not, fair): every leg active, two-sided, no wider than the cap,
    its game far enough from the start; the fair in range."""
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
        exp = _iso_s(m.get("expected_expiration_time"))
        if exp is None:
            return "no_start", None
        if exp - GAME_HOURS * 3600 - now_s < cfg.min_lead_min * 60:
            return "starts_soon", None
        mid = (bid + ask) / 2.0
        probs.append(mid if side == "yes" else 1.0 - mid)
    fair = math.prod(probs)
    if not (cfg.min_fair <= fair <= cfg.max_fair):
        return "fair_range", fair
    return None, fair


def quote_price(fair: float, cfg: Config, grid=None):
    """(our YES price, the no_bid we post): fair x (1 + markup) rounded UP."""
    ask = grid_up(fair * (1.0 + cfg.markup), grid or DEFAULT_GRID)
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
    for g in games:
        if book.game_payout(g) + contracts > cfg.max_game_payout + 1e-9:
            return "game_cap"
    if book.open_loss() + contracts * (1.0 - ask) > cfg.max_open_loss + 1e-9:
        return "loss_cap"
    if book.day_count(day) >= cfg.max_tickets:
        return "ticket_cap"
    return None


def confirm_check(q: dict, markets: dict, now_s: float, cfg: Config, book: Book, contracts: float):
    """The last look: (why not, fair now)."""
    why, fair = price_check(q["legs"], markets, now_s, cfg)
    if why:
        return why, fair
    if q["ask"] < fair * (1.0 + cfg.confirm_min_edge) - 1e-9:
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


class Maker:
    def __init__(self, client, cfg: Config, writer, book: Book, out_dir: str, clock=time.time):
        self.c = client
        self.cfg = cfg
        self.w = writer
        self.book = book
        self.out_dir = out_dir
        self.clock = clock
        self.cache = {}                 # leg ticker -> (read at, market)
        self.quotes = {}                # quote id -> our quote
        self.sent = deque()             # quote send times (rate cap)
        self.reads = deque()            # leg read times (rate cap)
        self.st = {}
        self.direction_checks = 0
        self.pause_until = 0.0
        self.errors = 0
        self.last_settle = 0.0
        self._reset_stats()

    def _reset_stats(self):
        self.st = {"rfq": 0, "pre_ok": 0, "priced": 0, "quoted": 0, "dry": 0, "accepted": 0,
                   "confirmed": 0, "declined": 0, "executed": 0, "errors": 0, "why": {}}
        self.st_since = self.clock()

    def _why(self, w):
        self.st["why"][w] = self.st["why"].get(w, 0) + 1

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
        return {t: self.cache[t][1] for t in tickers if t in self.cache}

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
            read_ms = (time.time() - t0) * 1000
        except Exception as e:
            self._error("leg read", e)
            return
        why, fair = price_check(legs, mk, now, self.cfg)
        if why:
            self._why(why)
            return
        self.st["priced"] += 1
        ask, no_bid = quote_price(fair, self.cfg)
        cts = contracts_of(rfq, ask)
        games = [game_of(l[0]) for l in legs]
        why = caps_check(self.book, games, cts, ask, self.cfg, et_day(now))
        if why:
            self._why(why)
            return
        q = dict(rfq, fair=fair, ask=ask, no_bid=no_bid, contracts=cts, games=games, at=now,
                 lag_ms=None if rfq["rfq_ms"] is None else now * 1000 - rfq["rfq_ms"], read_ms=read_ms)
        if not self.cfg.live:
            self.st["dry"] += 1
            self.sent.append(now)                       # DRY keeps LIVE's quote rate
            self.w.write(dict(q, k="would_quote"))
            return
        try:
            body = {"rfq_id": rfq["id"], "yes_bid": "0", "no_bid": f"{no_bid:.4f}", "rest_remainder": False}
            js = self.c.post(path="/communications/quotes", body=json.dumps(body))
        except Exception as e:
            self._error("quote", e)
            return
        qid = js.get("id")
        self.sent.append(now)
        self.st["quoted"] += 1
        self.quotes[qid] = dict(q, quote_id=qid)
        self.w.write(dict(q, k="quote", quote_id=qid, sent_ms=(time.time() - now) * 1000))

    def on_accepted(self, msg: dict) -> None:
        qid = msg.get("quote_id")
        q = self.quotes.get(qid)
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
            mk = self.markets([l[0] for l in q["legs"]], 0.0)       # fresh: the last look
        except Exception as e:
            self._error("confirm read", e)
            self._decline(q, "read_failed", None, cts)
            return
        why, fair_now = confirm_check(q, mk, now, self.cfg, self.book, cts)
        if why:
            self._decline(q, why, fair_now, cts)
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
        self.errors += 1
        self.st["errors"] += 1
        body = str(getattr(e, "body", "") or e)[:300]
        self.w.write({"k": "err", "what": what, "err": f"{type(e).__name__}: {body}"})
        log(f"! {what}: {type(e).__name__}: {body[:160]}")
        low = body.lower()
        if "insufficient" in low:
            self.pause_until = self.clock() + 300
            log("! the combo shard is short of cash: quoting paused 5 min")
        elif self.errors % 10 == 0:
            self.pause_until = self.clock() + 60

    # ---- upkeep -----------------------------------------------------------------

    def upkeep(self) -> None:
        now = self.clock()
        for qid in [k for k, q in self.quotes.items() if now - q["at"] > QUOTE_TTL_S]:
            del self.quotes[qid]
        if self.direction_checks < DIRECTION_CHECKS:      # the first fills' side, read back
            t = next((x for x in self.book.tickets if x.get("status") == "executed"
                      and "dir_ok" not in x and now - x.get("executed_at", now) >= 3.0), None)
            if t is not None:
                self.check_direction(t)
        if self.cfg.live and now - self.last_settle >= SETTLE_EVERY_S:
            self.last_settle = now
            self.settle()
        if now - self.st_since >= STAT_SECS:
            rec = dict(self.st, k="stat", t=now, open=len(self.book.open()),
                       open_loss=round(self.book.open_loss(), 2), live=self.cfg.live)
            self.w.write(rec)
            top = sorted(self.st["why"].items(), key=lambda kv: -kv[1])[:4]
            log(f"min: rfq {self.st['rfq']} pre-ok {self.st['pre_ok']} priced {self.st['priced']} "
                + (f"quoted {self.st['quoted']} accepted {self.st['accepted']} confirmed "
                   f"{self.st['confirmed']} declined {self.st['declined']}" if self.cfg.live
                   else f"would quote {self.st['dry']}")
                + f" | open {len(self.book.open())} can lose ${self.book.open_loss():,.2f} | top skips "
                + ", ".join(f"{k} {v}" for k, v in top))
            self._reset_stats()

    def settle(self) -> None:
        open_t = [t for t in self.book.tickets if t.get("status") == "executed"]
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
    if cfg.live:
        cash = ks.shard_balances(client).get(1, 0.0)
        log(f"LIVE: combo shard (1) cash ${cash:,.2f}")
        if cash < 1.0:
            log("the combo shard holds no cash: fund it first (python kalshi_subaccount.py shard 300 --to 1)")
            lock.close()
            return 2
    writer = DayWriter(out_dir)
    book = Book(os.path.join(out_dir, STATE_NAME))
    mk = Maker(client, cfg, writer, book, out_dir)
    writer.write({"k": "start", "t": time.time(), "live": cfg.live, "cfg": vars(cfg)})
    log(("LIVE" if cfg.live else "DRY (nothing is sent)") + (", NFL ONLY (fee-free)" if cfg.nfl_only else "")
        + f": markup {cfg.markup:.0%} (confirm >= {cfg.confirm_min_edge:.1%}), "
        f"{cfg.min_legs}-{cfg.max_legs} main-line legs, fair {cfg.min_fair:.0%}-{cfg.max_fair:.0%}, "
        f"payout <= ${cfg.max_payout:,.0f}/ticket, ${cfg.max_game_payout:,.0f}/game, open loss <= "
        f"${cfg.max_open_loss:,.0f}, {cfg.max_tickets} tickets/day; open now {len(book.open())}")
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
    ap.add_argument("--markup", type=float, default=d.markup)
    ap.add_argument("--max-payout", type=float, default=d.max_payout)
    ap.add_argument("--max-game-payout", type=float, default=d.max_game_payout)
    ap.add_argument("--max-open-loss", type=float, default=d.max_open_loss)
    ap.add_argument("--max-tickets", type=int, default=d.max_tickets)
    ap.add_argument("--nfl-only", action="store_true",
                    help="only all-NFL combos (no maker fee by Kalshi's table); allows --markup 0.03")
    ap.add_argument("--shard-factor", type=int, default=d.shard_factor,
                    help="see 1 in N of the RFQs (50 = 2%%; 10 = 10%%, ~470 KB/s on the Wi-Fi)")
    a = ap.parse_args(argv)
    floor = 0.03 if a.nfl_only else 0.05
    if a.markup < floor:
        ap.error(f"markup under {floor:.0%}: " + ("too thin even fee-free" if a.nfl_only else
                 "it does not cover the 0.035 p(1-p) maker fee with room (all-NFL combos are "
                 "fee-free: --nfl-only allows 3%)"))
    cfg = Config(live=a.live, markup=a.markup, max_payout=a.max_payout,
                 max_game_payout=a.max_game_payout, max_open_loss=a.max_open_loss,
                 max_tickets=a.max_tickets, nfl_only=a.nfl_only, shard_factor=a.shard_factor)
    if not 1 <= cfg.shard_factor <= 100:
        ap.error("--shard-factor 1-100")
    return run(cfg, a.minutes)


if __name__ == "__main__":
    sys.exit(main())
