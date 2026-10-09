"""Tests for nfl_snipe_bot (the NFL prop taker). No network: the exchange,
the model watch and the kickoff feed are fakes."""

import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

# never send real email from the suite: the machine's user env carries the
# bots' Gmail credentials (TestEmail sets its own environment)
os.environ["SNIPE_EMAIL"] = "0"

import nfl_prop_fair as nf
import nfl_snipe_bot as sb

KICK = datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc).timestamp()
NOW = KICK - 3 * 3600                       # 14:00Z, three hours out
T = "KXNFLLADDERREC-26OCT04MIAMIN-MIAMWASHINGTON6"
T_ESC = "KXNFLESCALATORREC-26OCT04MIAMIN-MIAMWASHINGTON6"
T_YDS = "KXNFLESCALATORRECYDS-26OCT04TENBAL-TENTPOLLARD20"


def cfg(**kw):
    c = sb.Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def market(t=T, bid=None, ask=None, step="0.0100", shard=0):
    m = {"ticker": t, "event_ticker": t.rsplit("-", 1)[0],
         "price_ranges": [{"start": "0.0000", "end": "1.0000", "step": step}],
         "exchange_index": shard, "yes_sub_title": "Malik Washington"}
    if bid is not None:
        m["yes_bid_dollars"] = f"{bid / 100:.4f}"
    if ask is not None:
        m["yes_ask_dollars"] = f"{ask / 100:.4f}"
    return m


def entry(fair=15.0, lo=10.5, hi=22.5, mu=3.0, stat="rec", kind="ladder",
          **kw):
    e = {"stat": stat, "kind": kind, "player": "Malik Washington",
         "team": "MIA", "jersey": 6, "pid": "00-0039999", "mu": mu,
         "fair": fair / 100, "fair_lo": lo / 100, "fair_hi": hi / 100,
         "roster_ts": NOW - 60, "injury": None, "n_games": 30}
    e.update(kw)
    return e


class TestPriceMath(unittest.TestCase):
    def test_fee_and_ticks(self):
        self.assertAlmostEqual(sb.fee_cents(50), 1.75)
        self.assertAlmostEqual(sb.fee_cents(3), 0.2037)
        self.assertEqual(sb.tick_at(market(step="0.0001"), 3.0), 0.01)
        self.assertEqual(sb.tick_at(market(), 30.0), 1.0)
        self.assertEqual(sb.tick_at({"ticker": T}, 30.0), 1.0)
        self.assertEqual(sb.ceil_tick(13.001, 1.0), 14.0)
        self.assertEqual(sb.ceil_tick(13.0, 1.0), 13.0)
        self.assertEqual(sb.floor_tick(1.4671, 0.01), 1.46)
        self.assertEqual(sb.ceil_tick(1.4671, 0.01), 1.47)

    def test_sell_limit_clears_band_imm_cap_and_edge(self):
        c = cfg()
        # whole-cent: band top 22.5 -> margin 23.5, IMM cap floor(23.5)=23
        # -> over it 24; the 2c / 3%-of-risk edge vs fair 15 binds lower
        self.assertEqual(sb.sell_limit(15.0, 22.5, 1.0, c), 24.0)
        # a fair close under the band top: the edge binds over the IMM cap
        # (23: 3 - fee 1.24 = 1.76 < 2c; 24: 2.72 >= 2c and >= 3% x 76)
        self.assertEqual(sb.sell_limit(20.0, 21.0, 1.0, c), 24.0)
        # sub-penny: IMM cap 1.46 (0.46 + 1 on the 0.01c tick), the edge
        # (3% of ~98c risk ~ 2.9c over a 0.24c fair) binds at ~3.3c
        lim = sb.sell_limit(0.24, 0.46, 0.01, c)
        self.assertGreater(lim, 3.0)
        self.assertLess(lim, 3.6)
        self.assertAlmostEqual(lim, round(lim, 2))
        # nothing under 100 qualifies
        self.assertIsNone(sb.sell_limit(97.0, 98.0, 1.0, c))

    def test_buy_limit_mirror(self):
        c = cfg()
        # band bottom 13.3: margin -> 12.3, IMM ask floor ceil(12.3)=13 ->
        # under it 12; edge vs fair 19 (19 - U - fee >= max(2, 3% U)) fine
        self.assertEqual(sb.buy_limit(19.0, 13.3, 1.0, c), 12.0)
        # edge binds: fair 4, bottom 3.5 -> U <= 2.5 margin; 4 - U - fee >= 2
        self.assertEqual(sb.buy_limit(4.0, 3.5, 1.0, c), 1.0)
        self.assertIsNone(sb.buy_limit(2.0, 1.5, 1.0, c))

    def test_limits_never_reach_the_imm_cap(self):
        c = cfg(band_margin_cents=0.0, min_net_cents=0.0, min_ror=0.0)
        # with no margin or edge, the IMM cap (hi + 1c) still binds
        self.assertEqual(sb.sell_limit(5.0, 10.0, 1.0, c), 12.0)   # cap 11
        self.assertEqual(sb.sell_limit(0.2, 0.46, 0.01, c), 1.47)  # cap 1.46
        self.assertEqual(sb.buy_limit(30.0, 20.0, 1.0, c), 18.0)   # floor 19


class TestBooks(unittest.TestCase):
    def test_book_levels_and_sweep(self):
        ob = {"orderbook_fp": {"yes_dollars": [["0.3300", "50"], ["0.3400", "20"],
                                               ["0.2000", "500"]],
                               "no_dollars": [["0.6000", "100"], ["0.5900", "9"]]}}
        bids, asks = sb.book_levels(ob)
        self.assertEqual(bids, [(34.0, 20.0), (33.0, 50.0), (20.0, 500.0)])
        self.assertEqual(asks, [(40.0, 100.0), (41.0, 9.0)])
        self.assertEqual(sb.sweep(bids, 31.0, "sell", float("inf"))[0], 70.0)
        got, avg = sb.sweep(bids, 31.0, "sell", 30)
        self.assertEqual(got, 30)
        self.assertAlmostEqual(avg, (20 * 34 + 10 * 33) / 30)
        self.assertEqual(sb.sweep(asks, 40.5, "buy", 500), (100.0, 40.0))
        # our own order netted out of its level
        self.assertEqual(sb.net_out(bids, [(33.0, 50.0), (20.0, 100.0)]),
                         [(34.0, 20.0), (20.0, 400.0)])

    def test_risk_per_contract(self):
        self.assertAlmostEqual(sb.risk_per_contract("sell", 34.0), 0.66)
        self.assertAlmostEqual(sb.risk_per_contract("buy", 10.0), 0.10)


class TestSnipeBook(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_fills_risk_and_roundtrip(self):
        p = os.path.join(self.d, "b.json")
        b = sb.SnipeBook(p)
        b.apply_fill(T, "sell", 50, 34.0, 0.5, "pidA|MIA", KICK, NOW)
        self.assertEqual(b.pos(T), -50)
        self.assertAlmostEqual(b.risk(T), 50 * 0.66)            # 33.00
        b.apply_fill(T_ESC, "buy", 100, 8.0, 0.1, "pidA|MIA", KICK, NOW)
        self.assertAlmostEqual(b.risk(T_ESC), 8.0)
        self.assertAlmostEqual(b.player_risk("pidA|MIA"), 41.0)
        self.assertAlmostEqual(b.total_risk(), 41.0)
        b.note_order(NOW)
        b.note_order(NOW)
        b.save()
        b2 = sb.SnipeBook(p)
        self.assertEqual(b2.pos(T), -50)
        self.assertAlmostEqual(b2.total_risk(), 41.0)
        self.assertEqual(b2.orders_today(NOW), 2)
        self.assertEqual(b2.orders_today(NOW + 86400), 0)      # a new day
        self.assertEqual(b2.last_trade(T), NOW)
        # buying back the short flattens it out of the book
        b2.apply_fill(T, "buy", 50, 20.0, 0.2, "pidA|MIA", KICK, NOW + 5)
        self.assertNotIn(T, b2.d["positions"])
        self.assertEqual(b2.risk(T), 0.0)

    def test_prune_after_the_game(self):
        b = sb.SnipeBook(os.path.join(self.d, "b.json"))
        b.apply_fill(T, "sell", 10, 30.0, 0.0, "k", KICK, NOW)
        self.assertEqual(b.prune(KICK + 3600), [])
        self.assertEqual(b.prune(KICK + sb.PRUNE_AFTER_KICKOFF_SECS + 1), [T])
        self.assertEqual(b.total_risk(), 0.0)
        self.assertEqual(b.d["closed"][0]["ticker"], T)


class TestKickoff(unittest.TestCase):
    def test_matches_teams_across_code_spellings(self):
        calls = []

        def get(url):
            calls.append(url)
            if "20261004" in url:
                return {"events": [
                    {"date": "2026-10-04T17:00Z", "status": {"type": {"name": "STATUS_SCHEDULED"}},
                     "competitions": [{"competitors": [{"team": {"abbreviation": "CIN"}},
                                                       {"team": {"abbreviation": "JAX"}}]}]},
                    {"date": "2026-10-04T20:25Z", "status": {"type": {"name": "STATUS_POSTPONED"}},
                     "competitions": [{"competitors": [{"team": {"abbreviation": "MIA"}},
                                                       {"team": {"abbreviation": "MIN"}}]}]}]}
            return {"events": []}

        k = sb.KickoffResolver(get_json=get)
        self.assertEqual(k.kickoff("KXNFLLADDERREC-26OCT04JACCIN", NOW), KICK)
        self.assertIsNone(k.kickoff("KXNFLLADDERREC-26OCT04MIAMIN", NOW))  # postponed
        n = len(calls)
        self.assertEqual(k.kickoff("KXNFLESCALATORREC-26OCT04JACCIN", NOW), KICK)
        self.assertEqual(len(calls), n)                       # cached


class TestSignal(unittest.TestCase):
    def test_sell_and_buy_and_inside(self):
        c = cfg()
        s = sb.find_signal(T, market(bid=34, ask=98), entry(), c)
        self.assertEqual((s.side, s.top, s.limit), ("sell", 34.0, 24.0))
        self.assertAlmostEqual(s.net, 34 - 15 - sb.fee_cents(34))
        self.assertIsNone(sb.find_signal(T, market(bid=23, ask=98), entry(), c))
        b = sb.find_signal(T, market(bid=5, ask=10), entry(fair=19, lo=13.3, hi=28.5), c)
        self.assertEqual((b.side, b.top, b.limit), ("buy", 10.0, 12.0))
        self.assertIsNone(sb.find_signal(T, market(bid=34), entry(err="x"), c))
        self.assertIsNone(sb.find_signal(T, market(bid=34), entry(), cfg(sides="buy")))

    def test_recent_form_widens_the_band(self):
        # Golden 10/4: model 51 yds, last four 84 95 58 100, book 96 yds
        e = entry(stat="recyds", mu=50.8, fair=12.69, lo=8.9, hi=19.0)
        games = [{"receiving_yards": v} for v in (84, 95, 58, 100)]
        rm = sb.recent_mean(games, "recyds", 4)
        self.assertAlmostEqual(rm, 84.25)
        m = market(t="KXNFLLADDERRECYDS-26OCT04GBTB-GBMGOLDEN0", bid=24, ask=99)
        self.assertIsNotNone(sb.find_signal(m["ticker"], m, e, cfg()))
        rb = sb.recent_band(m, e, rm)
        self.assertAlmostEqual(rb[0], 84.25 * 0.25, places=1)  # $0.0025 a yard
        self.assertIsNone(sb.find_signal(m["ticker"], m, e, cfg(), recent=rb))
        # Pickens: book 136 yds, model 61.5, recent 40 -> still a sell, and
        # against the higher fair
        e2 = entry(stat="recyds", mu=61.5, fair=15.37, lo=10.76, hi=23.06)
        m2 = market(t="KXNFLLADDERRECYDS-26OCT04DALHOU-DALGPICKENS3", bid=34, ask=99)
        rb2 = sb.recent_band(m2, e2, 39.75)
        s = sb.find_signal(m2["ticker"], m2, e2, cfg(), recent=rb2)
        self.assertEqual(s.side, "sell")
        self.assertAlmostEqual(s.model_fair, 15.37)

    def test_recent_band_width_follows_the_model(self):
        # under FULL_GAMES of history the model's band is the WIDE one, and
        # so is the recent band (Jeremiyah Love, a rookie, 10/4)
        m = market(t="KXNFLLADDERRSHYDS-26OCT04ARINYG-ARIJLOVE4", bid=31, ask=99)
        rookie = entry(stat="rshyds", mu=52.2, fair=13.05, lo=6.53, hi=26.11, n_games=3)
        f, lo, hi = sb.recent_band(m, rookie, 53.3)
        self.assertAlmostEqual(hi, 53.3 * nf.WIDE_HI * 0.25, places=1)  # $0.0025/yd
        self.assertAlmostEqual(lo, 53.3 * nf.WIDE_LO * 0.25, places=1)
        vet = dict(rookie, n_games=30)
        self.assertAlmostEqual(sb.recent_band(m, vet, 53.3)[2], 53.3 * nf.BAND_HI * 0.25,
                               places=1)

    def test_entry_recent_prefers_the_models_fields(self):
        m = market(bid=34, ask=98)
        e = entry(recent_mean=3.75, fair_recent=0.1875, fair_recent_lo=0.13,
                  fair_recent_hi=0.40)
        rm, band = sb.entry_recent(m, e, [{"receptions": 9}] * 4, 4)
        self.assertEqual(rm, 3.75)                         # not the games' 9
        self.assertEqual(band, (18.75, 13.0, 40.0))
        e2 = entry()                                        # no model fields
        rm, band = sb.entry_recent(m, e2, [{"receptions": v} for v in (3, 3, 4, 5)], 4)
        self.assertEqual(rm, 3.75)
        self.assertAlmostEqual(band[2], 3.75 * nf.BAND_HI * 5.0, places=2)
        self.assertEqual(sb.entry_recent(m, e2, None, 4), (None, None))

    def test_recent_mean_clips_and_needs_three(self):
        self.assertEqual(sb.recent_mean([{"rushing_yards": -4}, {"rushing_yards": 10},
                                         {"rushing_yards": 20}], "rshyds", 4), 10.0)
        self.assertIsNone(sb.recent_mean([{"receptions": 3}, {"receptions": 4}], "rec", 4))


class TestSkipReasons(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.book = sb.SnipeBook(os.path.join(self.d, "b.json"))

    def sig(self, **kw):
        s = sb.find_signal(T, market(bid=34, ask=98), entry(), cfg())
        s.kickoff, s.implied_mu = KICK, 6.8
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def r(self, s=None, e=None, now=NOW, sibs=None, imm_fair=None, **c):
        return sb.skip_reason(s or self.sig(), e or entry(), now - 30, now,
                              sibs or {}, self.book, cfg(**c), imm_fair=imm_fair)

    def test_each_reason(self):
        self.assertEqual(self.r(), "")
        self.assertEqual(sb.skip_reason(self.sig(), entry(), NOW - 3600, NOW, {},
                                        self.book, cfg()), "stale_model")
        self.assertEqual(self.r(self.sig(kickoff=None)), "kickoff_unknown")
        self.assertEqual(self.r(now=KICK - 600), "too_late")
        self.assertEqual(self.r(now=KICK - 80 * 3600), "too_early")
        self.assertEqual(self.r(e=entry(roster_ts=NOW - 3600)), "stale_roster")
        self.assertEqual(self.r(e=entry(roster_ts=None)), "stale_roster")
        self.assertEqual(self.r(e=entry(injury="Questionable")), "injury")
        self.assertEqual(self.r(e=entry(injury="not on roster")), "injury")
        self.assertEqual(self.r(e=entry(news_at=NOW - 3600)), "team_news")
        self.assertEqual(self.r(e=entry(news_at=NOW - 13 * 3600)), "")
        self.assertEqual(self.r(imm_fair=20.0), "model_mismatch")   # 15 vs 20
        self.assertEqual(self.r(imm_fair=15.5), "")
        self.assertEqual(self.r(self.sig(implied_mu=None)), "no_implied_mean")
        self.assertEqual(self.r(self.sig(implied_mu=8.0)), "mu_ratio")  # 8 > 2.5 x 3
        self.assertEqual(self.r(sibs={T_ESC: 6.5}), "sibling")          # within 15%
        self.assertEqual(self.r(sibs={T_ESC: 4.0}), "")
        self.book.apply_fill(T, "sell", 10, 34.0, 0.0, "k", KICK, NOW - 60)
        self.assertEqual(self.r(), "cooldown")
        self.assertEqual(self.r(now=NOW + 1000), "")

    def test_sibling_means_ignore_wide_books(self):
        entries = {T: entry(), T_ESC: entry(kind="escalator", fair=2.5, lo=1, hi=5)}
        by = {(entries[T]["pid"], "rec"): [T, T_ESC]}
        mk = {T: market(bid=34, ask=98),
              T_ESC: market(t=T_ESC, bid=1.0, ask=40.0, step="0.0001")}
        self.assertEqual(sb.sibling_means(T, entries[T], entries, mk, by), {})
        mk[T_ESC] = market(t=T_ESC, bid=8.0, ask=9.0, step="0.0001")
        out = sb.sibling_means(T, entries[T], entries, mk, by)
        self.assertIn(T_ESC, out)
        self.assertGreater(out[T_ESC], 3.0)


class TestSizing(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.book = sb.SnipeBook(os.path.join(self.d, "b.json"))
        self.s = sb.find_signal(T, market(bid=34, ask=98), entry(), cfg())

    def test_caps(self):
        c = cfg()
        per = 0.76 + sb.fee_cents(24) / 100        # risk at the 24c limit
        n, why = sb.size_order(self.s, 24.0, 1000, self.book, 5000.0, c)
        self.assertEqual((n, why), (int(40 / per), ""))          # $40 market cap
        n, why = sb.size_order(self.s, 24.0, 30, self.book, 5000.0, c)
        self.assertEqual(n, 30)                                    # the book binds
        n, why = sb.size_order(self.s, 24.0, 3, self.book, 5000.0, c)
        self.assertEqual((n, why), (0, "thin"))
        n, why = sb.size_order(self.s, 24.0, 1000, self.book, 502.0, c)
        self.assertEqual((n, why), (0, "cash"))                    # $2 over the floor
        n, _ = sb.size_order(self.s, 24.0, 1000, self.book, None, c)
        self.assertGreater(n, 0)                                   # unknown cash: caps only
        self.book.apply_fill(T, "sell", 50, 34.0, 0.0, self.s.player_key, KICK, NOW)
        n, why = sb.size_order(self.s, 24.0, 1000, self.book, 5000.0, c)
        self.assertEqual((n, why), (int(7.0 / per), ""))         # $33 of $40 used
        self.book.apply_fill(T, "sell", 10, 34.0, 0.0, self.s.player_key, KICK, NOW)
        n, why = sb.size_order(self.s, 24.0, 1000, self.book, 5000.0, c)
        self.assertEqual((n, why), (0, "market_cap"))             # $39.60 used
        n, why = sb.size_order(self.s, 24.0, 1000, self.book, 5000.0,
                               cfg(max_risk_market=1000, max_risk_player=41))
        self.assertEqual((n, why), (0, "player_cap"))


# ------------------------------------------------------------- end to end

class FakeExchange:
    def __init__(self, markets, books, own=None, cash=2000.0, resp=None, booked=None):
        self.markets, self.books = markets, books
        self.own = own or {}
        self.cash = cash
        self.resp = resp
        self.placed = []
        self.subaccount = 0
        self.booked = booked
        self.api_base = "fake"

    def order_subaccount(self, order_id):
        return self.booked

    def open_markets(self):
        return list(self.markets.values())

    def orderbook(self, t):
        return self.books[t]

    def own_resting(self, t):
        return self.own.get(t, [])

    def free_cash(self, shard=0):
        return self.cash

    def place_ioc(self, ticker, side, count, limit_cents, coid):
        self.placed.append((ticker, side, count, limit_cents, coid))
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp or {"order_id": "o1", "fill_count": f"{count:.2f}",
                             "average_fill_price": "0.3400",
                             "average_fee_paid": "0.0158"}


class FakeIndex:
    def __init__(self, games):
        self.games = games


class FakeWatch:
    def __init__(self, entries, games=None, ts=NOW - 10):
        self.entries, self.ts = entries, ts
        self.index = FakeIndex(games or {})
        self.status_seen, self.status_changed_at = {}, {}

    def refresh(self, now_ts=None):
        return {"ts": self.ts, "markets": self.entries, "errors": []}


class FakeKick:
    def kickoff(self, ev, now_ts=None):
        return KICK


# recent form 3.75 catches: band top 5.6 x 5c = 28.1 -> limit 30c (the
# IMM-cap rule over the wider band); $40 / (70c + fee(30)) contracts
CAPPED = int(40.0 / (0.70 + sb.fee_cents(30.0) / 100.0))


class TestScan(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        p = mock.patch.object(sb, "IMM_STATUS_FILE", os.path.join(self.d, "none.json"))
        p.start()
        self.addCleanup(p.stop)

    def bot(self, live=False, ex=None, entries=None, **c):
        entries = entries if entries is not None else {T: entry()}
        ex = ex or FakeExchange(
            {T: market(bid=34, ask=98)},
            {T: {"orderbook_fp": {"yes_dollars": [["0.3400", "20"], ["0.3300", "40"],
                                                  ["0.1400", "900"]],
                                  "no_dollars": [["0.0200", "500"]]}}})
        games = {entry()["pid"]: [{"receptions": v} for v in (3, 3, 4, 5)]}
        b = sb.Sniper(cfg(live=live, **c), exchange=ex, watch=FakeWatch(entries, games),
                      kick=FakeKick(), log_dir=self.d, log=sb.Log(self.d, echo=False))
        return b, ex

    def test_dry_run_books_a_paper_fill_and_sends_nothing(self):
        b, ex = self.bot()
        out = b.scan(NOW)
        self.assertEqual(ex.placed, [])
        self.assertEqual(len(out["taken"]), 1)
        tk = out["taken"][0]
        self.assertTrue(tk["dry"])
        # 60 over the 30c limit (20 @ 34, 40 @ 33); $40 at 70c + fee a contract
        self.assertEqual(tk["count"], CAPPED)
        self.assertEqual(b.book.pos(T), -CAPPED)
        self.assertTrue(os.path.exists(os.path.join(self.d, sb.PAPER_BOOK_FILE)))
        self.assertFalse(os.path.exists(os.path.join(self.d, sb.BOOK_FILE)))
        # the next scan: cooldown on the market
        out = b.scan(NOW + 30)
        self.assertEqual(out["skips"].get("cooldown"), 1)

    def test_live_sends_one_ioc_and_books_the_fill(self):
        b, ex = self.bot(live=True)
        out = b.scan(NOW)
        self.assertEqual(len(ex.placed), 1)
        t, side, n, lim, coid = ex.placed[0]
        # recent form 3.75 catches x1.5 = 5.6 -> band top 28.1 -> the limit
        # clears that band and the IMM cap over it
        self.assertEqual((t, side, n, lim), (T, "sell", CAPPED, 30.0))
        self.assertTrue(coid.startswith("snp-"))
        self.assertEqual(b.book.pos(T), -CAPPED)
        self.assertEqual(out["taken"][0]["filled"], float(CAPPED))
        with open(os.path.join(self.d, sb.BOOK_FILE), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["positions"][T], -CAPPED)

    def test_never_sweeps_into_our_own_bid(self):
        ex = FakeExchange(
            {T: market(bid=34, ask=98)},
            {T: {"orderbook_fp": {"yes_dollars": [["0.3400", "20"], ["0.3300", "140"]],
                                  "no_dollars": []}}},
            own={T: [("bid", 33.0, 100.0)]})
        b, ex = self.bot(live=True, ex=ex)
        b.scan(NOW)
        _t, _s, n, lim, _c = ex.placed[0]
        self.assertEqual(lim, 34.0)                  # a tick over our 33c bid
        self.assertEqual(n, 20)                      # only the external 20 @ 34
        # (and 40 external @ 33 left alone: the sweep would reach our bid)

    def test_halt_file_stops_everything(self):
        b, ex = self.bot(live=True)
        open(os.path.join(self.d, sb.HALT_FILE), "w").close()
        out = b.scan(NOW)
        self.assertTrue(out.get("halted"))
        self.assertEqual(ex.placed, [])

    def test_refused_order_logs_kalshis_reason(self):
        err = RuntimeError("HttpError(400 Bad Request)")
        err.body = '{"error":{"code":"invalid_order","details":"market paused"}}'
        b, ex = self.bot(live=True, ex=FakeExchange(
            {T: market(bid=34, ask=98)},
            {T: {"orderbook_fp": {"yes_dollars": [["0.3400", "60"]], "no_dollars": []}}},
            resp=err))
        out = b.scan(NOW)
        self.assertEqual(out["skips"].get("refused"), 1)
        self.assertEqual(b.book.pos(T), 0.0)
        with open(os.path.join(self.d, f"orders_{sb._utc().strftime('%Y-%m-%d')}.jsonl"),
                  encoding="utf-8") as f:
            row = json.loads(f.readline())
        self.assertIn("market paused", row["error_body"])

    def test_injured_player_and_imm_news_skip(self):
        b, ex = self.bot(entries={T: entry(injury="Questionable")})
        self.assertEqual(b.scan(NOW)["skips"], {"injury": 1})
        # the IMM's snapshot saw a teammate's designation cleared 20 min ago
        imm = os.path.join(self.d, "imm.json")
        with open(imm, "w", encoding="utf-8") as f:
            json.dump({"ts": NOW, "teams": {"mia": {"news_at": NOW - 1200}},
                       "markets": {T: {"fair": 0.15}}}, f)
        with mock.patch.object(sb, "IMM_STATUS_FILE", imm):
            b, ex = self.bot()
            self.assertEqual(b.scan(NOW)["skips"], {"team_news": 1})

    def test_scan_uses_the_models_recent_band(self):
        # the entry's own recent band (top 40c) covers the 34c book: no trade,
        # whatever the watch's games would say (3/3/4/5 -> top 28c)
        b, ex = self.bot(entries={T: entry(recent_mean=8.0, fair_recent=0.40,
                                           fair_recent_lo=0.28, fair_recent_hi=0.60)})
        out = b.scan(NOW)
        self.assertEqual(out["skips"], {"recent_form": 1})
        self.assertEqual(ex.placed, [])

    def test_recent_form_skip_counts(self):
        games = {entry()["pid"]: [{"receptions": v} for v in (7, 8, 6, 7)]}
        b, ex = self.bot()
        b.watch.index = FakeIndex(games)
        out = b.scan(NOW)
        self.assertEqual(out["skips"], {"recent_form": 1})
        self.assertEqual(out["taken"], [])

    def test_watch_history_survives_a_restart(self):
        b, _ = self.bot()
        b.watch.status_seen[("mia", "malikwashington")] = "Questionable"
        b.watch.status_changed_at[("mia", "malikwashington")] = NOW - 100
        b.scan(NOW)
        b2, _ = self.bot()
        self.assertEqual(b2.watch.status_seen[("mia", "malikwashington")], "Questionable")
        self.assertEqual(b2.watch.status_changed_at[("mia", "malikwashington")], NOW - 100)

    def test_orders_per_scan_cap(self):
        ms, bks, es = {}, {}, {}
        for j in range(5):
            t = f"KXNFLLADDERREC-26OCT04MIAMIN-MIAMPLAYER{j}"
            ms[t] = market(t=t, bid=34, ask=98)
            bks[t] = {"orderbook_fp": {"yes_dollars": [["0.3400", "10"]], "no_dollars": []}}
            es[t] = entry(pid=f"p{j}")
        b, ex = self.bot(live=True, ex=FakeExchange(ms, bks), entries=es)
        b.scan(NOW)
        self.assertEqual(len(ex.placed), 3)


class TestBookFile(unittest.TestCase):
    def test_which_file(self):
        self.assertEqual(sb.book_file(cfg()), sb.PAPER_BOOK_FILE)
        self.assertEqual(sb.book_file(cfg(live=True)), sb.BOOK_FILE)
        self.assertEqual(sb.book_file(cfg(live=True, subaccount=3)), "snipe_book_sub3.json")
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        b = sb.SnipeBook(os.path.join(d, "x.json"), subaccount=3)
        b.save()
        with open(os.path.join(d, "x.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["subaccount"], 3)


class TestSubaccountLive(unittest.TestCase):
    """Jack 2026-10-08: "yes subaccount, $250 at risk"."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        p = mock.patch.object(sb, "IMM_STATUS_FILE", os.path.join(self.d, "none.json"))
        p.start()
        self.addCleanup(p.stop)

    def bot(self, booked):
        ex = FakeExchange(
            {T: market(bid=34, ask=98)},
            {T: {"orderbook_fp": {"yes_dollars": [["0.3400", "60"]], "no_dollars": []}}},
            booked=booked)
        games = {entry()["pid"]: [{"receptions": v} for v in (3, 3, 4, 5)]}
        b = sb.Sniper(cfg(live=True, subaccount=3), exchange=ex,
                      watch=FakeWatch({T: entry()}, games), kick=FakeKick(),
                      log_dir=self.d, log=sb.Log(self.d, echo=False))
        return b, ex

    def test_booked_to_our_subaccount_trades_on(self):
        b, ex = self.bot(booked=3)
        out = b.scan(NOW)
        self.assertEqual(len(out["taken"]), 1)
        self.assertFalse(os.path.exists(os.path.join(self.d, sb.HALT_FILE)))
        self.assertTrue(os.path.exists(os.path.join(self.d, "snipe_book_sub3.json")))

    def test_booked_elsewhere_halts_and_books_the_primary(self):
        b, ex = self.bot(booked=0)
        out = b.scan(NOW)
        self.assertEqual(out["skips"].get("wrong_subaccount"), 1)
        self.assertTrue(os.path.exists(os.path.join(self.d, sb.HALT_FILE)))
        # the fill sits in the primary's book, which the IMM nets out
        with open(os.path.join(self.d, sb.BOOK_FILE), encoding="utf-8") as f:
            js = json.load(f)
        self.assertLess(js["positions"][T], 0)
        self.assertEqual(js["subaccount"], 0)
        # ... and the next scan does nothing
        self.assertTrue(b.scan(NOW + 1000).get("halted"))

    def test_free_cash_reads_the_subaccounts_own_balance(self):
        got = {}

        class C:
            def get(self, path, params):
                got["path"], got["params"] = path, params
                return {"balance_dollars": "300.0000",
                        "balance_breakdown": [{"balance": "18373.1369", "exchange_index": 0}]}

        ex = sb.Exchange(subaccount=2)
        ex._acct = C()
        self.assertEqual(ex.free_cash(0), 300.0)        # not the account-wide 18,373
        self.assertEqual(got, {"path": "/portfolio/balance", "params": {"subaccount": 2}})

    def test_order_subaccount_read(self):
        class C:
            def get(self, path, params):
                if params:
                    raise RuntimeError("404")          # not in the subaccount
                return {"order": {"order_id": "x", "subaccount_number": 0}}

        ex = sb.Exchange(subaccount=2)
        ex._acct = C()
        self.assertEqual(ex.order_subaccount("x"), 0)

    def test_live_refuses_an_unfunded_subaccount(self):
        with mock.patch.object(sb.Exchange, "free_cash", return_value=0.0), \
                mock.patch.object(sb, "Sniper") as S:
            self.assertEqual(sb.main(["--live", "--subaccount", "2",
                                      "--log-dir", self.d]), 2)
            S.assert_not_called()


class TestEmail(unittest.TestCase):
    """Jack 2026-10-08: "email alert everytime it buys"."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        p = mock.patch.object(sb, "IMM_STATUS_FILE", os.path.join(self.d, "none.json"))
        p.start()
        self.addCleanup(p.stop)

    def test_every_live_fill_mails(self):
        sent = []
        ex = FakeExchange(
            {T: market(bid=34, ask=98)},
            {T: {"orderbook_fp": {"yes_dollars": [["0.3400", "60"]], "no_dollars": []}}},
            booked=1)
        games = {entry()["pid"]: [{"receptions": v} for v in (3, 3, 4, 5)]}
        b = sb.Sniper(cfg(live=True, subaccount=1), exchange=ex,
                      watch=FakeWatch({T: entry()}, games), kick=FakeKick(),
                      log_dir=self.d, log=sb.Log(self.d, echo=False))
        b.mailer = lambda s, body: sent.append((s, body)) or True
        b.scan(NOW)
        for _ in range(50):                        # the mail runs on a thread
            if sent:
                break
            time.sleep(0.02)
        self.assertEqual(len(sent), 1)
        subject, body = sent[0]
        self.assertIn("SOLD", subject)
        self.assertIn("Malik Washington", subject)
        self.assertIn(T, body)
        self.assertIn("Subaccount 1", body)

    def test_dry_run_and_no_fill_send_nothing(self):
        b = sb.Sniper(cfg(), exchange=FakeExchange({}, {}), watch=FakeWatch({}),
                      kick=FakeKick(), log_dir=self.d, log=sb.Log(self.d, echo=False))
        self.assertIsNone(b.mailer)                # dry run: no mailer at all

    def test_send_email_uses_the_alert_account(self):
        calls = {}

        class S:
            def __init__(self, host, port, timeout):
                calls["host"] = (host, port)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                calls["login"] = u

            def sendmail(self, frm, to, msg):
                calls["to"] = to

        env = {"ALERT_EMAIL_FROM": "bot@x.com", "ALERT_EMAIL_PASSWORD": "pw",
               "SNIPE_ALERT_TO": "jack@x.com", "SNIPE_EMAIL": "1"}
        with mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(sb.send_email("s", "b", smtp=S))
            self.assertTrue(sb.email_enabled())
        self.assertEqual(calls, {"host": ("smtp.gmail.com", 465), "login": "bot@x.com",
                                 "to": ["jack@x.com"]})
        with mock.patch.dict(os.environ, {"ALERT_EMAIL_FROM": "", "ALERT_EMAIL_PASSWORD": ""}):
            self.assertFalse(sb.send_email("s", "b", smtp=S))
        with mock.patch.dict(os.environ, {**env, "SNIPE_EMAIL": "0"}):
            self.assertFalse(sb.email_enabled())

        class Boom(S):
            def login(self, u, p):
                raise OSError("smtp down")
        with mock.patch.dict(os.environ, env):
            self.assertFalse(sb.send_email("s", "b", smtp=Boom))   # never raises


class TestExchangeBody(unittest.TestCase):
    def test_place_ioc_body(self):
        sent = {}

        class C:
            events_orders_url = "/portfolio/events/orders"

            def post(self, path, body):
                sent["path"], sent["body"] = path, json.loads(body)
                return {"order_id": "x", "fill_count": "0.00"}

        with mock.patch.object(sb.Exchange, "acct", lambda self: C()):
            sb.Exchange(subaccount=0).place_ioc(T, "sell", 60, 24.0, "snp-1")
            self.assertEqual(sent["body"], {
                "ticker": T, "side": "ask", "count": "60.00", "price": "0.2400",
                "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross",
                "client_order_id": "snp-1"})
            sb.Exchange(subaccount=2).place_ioc(T_YDS, "buy", 7, 3.47, "snp-2")
            self.assertEqual((sent["body"]["side"], sent["body"]["price"],
                              sent["body"]["subaccount"]), ("bid", "0.0347", 2))

    def test_free_cash_by_shard(self):
        js = {"balance": 186327, "balance_dollars": "1863.2765",
              "balance_breakdown": [{"balance": "1293.4041", "exchange_index": 0},
                                    {"balance": "569.8724", "exchange_index": 2}]}
        with mock.patch.object(sb.Exchange, "_get", staticmethod(lambda *a, **k: js)):
            ex = sb.Exchange()
            self.assertAlmostEqual(ex.free_cash(0), 1293.4041)
            self.assertAlmostEqual(ex.free_cash(2), 569.8724)


class TestPnl(unittest.TestCase):
    def test_settlement_and_marks(self):
        self.assertEqual(sb.settlement_cents({"status": "settled",
                                              "settlement_value_dollars": "0.2875"}), 28.75)
        self.assertIsNone(sb.settlement_cents({"status": "active",
                                               "settlement_value_dollars": "0.2875"}))
        self.assertEqual(sb.settlement_cents({"status": "finalized", "result": "no"}), 0.0)
        self.assertEqual(sb.mark_cents({"yes_bid_dollars": "0.2000",
                                        "yes_ask_dollars": "0.3000"}), 25.0)
        self.assertEqual(sb.mark_cents({"yes_bid_dollars": "0.2000"}), 20.0)
        self.assertIsNone(sb.mark_cents({}))
        # sold 50 at 34c, settled 15c: +$9.50 less the fee
        self.assertAlmostEqual(sb.trade_pnl("sell", 50, 34.0, 0.5, 15.0), 9.0)
        self.assertAlmostEqual(sb.trade_pnl("buy", 10, 10.0, 0.1, 19.0), 0.8)

    def test_report_from_paper_rows(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        rows = [
            {"ts": "2026-10-04T16:42:00+00:00", "ticker": "A", "player": "a", "side": "sell",
             "count": 60, "dry": True, "filled": 60, "avg_cents": 30.0, "fair": 15.0,
             "expected_net_cents": 13.5},
            {"ts": "2026-10-04T16:43:00+00:00", "ticker": "B", "player": "b", "side": "sell",
             "count": 300, "dry": True, "filled": 300, "avg_cents": 20.0, "fair": 10.0,
             "expected_net_cents": 8.9},
            {"ts": "2026-10-04T16:44:00+00:00", "ticker": "C", "dry": False, "filled": 9,
             "side": "sell", "avg_cents": 5.0},                 # a live row: not paper
        ]
        with open(os.path.join(d, "orders_2026-10-04.jsonl"), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        trades = sb.load_trades(d, "paper")
        self.assertEqual([t["ticker"] for t in trades], ["A", "B"])
        mk = {"A": {"status": "settled", "settlement_value_dollars": "0.1000"},
              "B": {"status": "active", "yes_bid_dollars": "0.1800",
                    "yes_ask_dollars": "0.2200"}}
        rep = sb.pnl_report(trades, lambda t: mk[t], default_cap=100.0)
        a, b = rep["rows"]
        fee_a = 60 * sb.fee_cents(30.0) / 100
        self.assertAlmostEqual(a["pnl"], 60 * 0.20 - fee_a)
        self.assertTrue(a["settled"])
        self.assertTrue(a["in_default_cap"])                    # $42 of risk
        self.assertFalse(b["settled"])
        self.assertAlmostEqual(b["value_cents"], 20.0)
        self.assertFalse(b["in_default_cap"])                   # +$240 > $100
        self.assertEqual(rep["all"]["settled"], 1)
        self.assertEqual(rep["all"]["open"], 1)
        self.assertEqual(rep["default_cap"]["trades"], 1)
        self.assertAlmostEqual(rep["all"]["settled_expected"], 60 * 0.135, places=2)

    def test_parse_until(self):
        self.assertEqual(sb.parse_until("2026-10-05T00:06Z"),
                         datetime(2026, 10, 5, 0, 6, tzinfo=timezone.utc).timestamp())
        self.assertEqual(sb.parse_until("1791000000"), 1791000000.0)
        self.assertIsNone(sb.parse_until(None))


class TestConfig(unittest.TestCase):
    def test_from_env(self):
        c = sb.Config.from_env({"SNIPE_LIVE": "1", "SNIPE_MAX_RISK_TOTAL": "500",
                                "SNIPE_MAX_ORDERS_PER_SCAN": "2", "SNIPE_SIDES": "SELL",
                                "SNIPE_MIN_ROR": "bad"})
        self.assertTrue(c.live)
        self.assertEqual(c.max_risk_total, 500.0)
        self.assertEqual(c.max_orders_per_scan, 2)
        self.assertEqual(c.sides, "sell")
        self.assertEqual(c.min_ror, sb.Config().min_ror)

    def test_imm_view(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, "s.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"ts": NOW - 3600, "teams": {"bal": {"news_at": NOW - 99}},
                       "markets": {T: {"fair": 0.15}}}, f)
        news, fairs = sb.imm_view(p, NOW)
        self.assertEqual(news, {"bal": NOW - 99})
        self.assertEqual(fairs, {})                      # an hour old: no fairs
        news, fairs = sb.imm_view(p, NOW - 3000)
        self.assertEqual(fairs, {T: 15.0})
        self.assertEqual(sb.imm_view(os.path.join(d, "missing"), NOW), ({}, {}))


if __name__ == "__main__":
    unittest.main()
