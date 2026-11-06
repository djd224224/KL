"""Tests for combo_rfq_logger.py and combo_rfq_score.py (the read-only combo
maker shadow, 2026-11-05). No network: the logger's message side and the
scorer's pricing, fills and report run on synthetic messages and markets."""
import gzip
import json
import os
import tempfile
import unittest
import zlib
from datetime import datetime, timezone

import combo_rfq_logger as cl
import combo_rfq_score as cs

T0 = 1793920000.0           # 2026-11-05 ~23:06Z


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


class ListWriter:
    def __init__(self):
        self.rows = []

    def write(self, obj):
        self.rows.append(obj)

    def kinds(self, k):
        return [r for r in self.rows if r.get("k") == k]


def rfq_msg(rid, mt, legs, ts, tc="10.0000", cn="0.00", seq=1):
    return {"type": "rfq_created", "sid": 1, "seq": seq,
            "msg": {"id": rid, "creator_id": "abcdef0123456789", "market_ticker": mt,
                    "target_cost_dollars": tc, "contracts_fp": cn, "created_ts": iso(ts),
                    "mve_collection_ticker": "KXMVECROSSCATEGORY-R",
                    "mve_selected_legs": [{"event_ticker": l[0].rsplit("-", 1)[0], "market_ticker": l[0],
                                           "side": l[1]} for l in legs]}}


def trade_msg(mt, ts, px="0.2500", ct="40.00", side="yes", seq=1, tid="t1"):
    return {"type": "trade", "sid": 2, "seq": seq,
            "msg": {"trade_id": tid, "market_ticker": mt, "yes_price_dollars": px, "count_fp": ct,
                    "taker_outcome_side": side, "taker_book_side": "bid", "is_block_trade": False,
                    "ts": int(ts), "ts_ms": int(ts * 1000)}}


LEGS = [["KXNBAGAME-26NOV05MIAGSW-GSW", "yes"], ["KXNHLGAME-26NOV05BOSTOR-BOS", "yes"]]
COMBO = "KXMVECROSSCATEGORY-S2026AAAA-BBBB"


class LoggerTests(unittest.TestCase):

    def make(self, now=T0, **kw):
        w = ListWriter()
        clock = [now]
        lg = cl.ComboLogger(w, rfq_sample=1, clock=lambda: clock[0], **kw)
        return lg, w, clock

    def test_a_print_inside_the_window_is_an_execution_with_its_legs(self):
        lg, w, _ = self.make()
        lg.handle(rfq_msg("r1", COMBO, LEGS, T0))
        lg.handle(trade_msg(COMBO, T0 + 2.5))
        ex = w.kinds("exec")
        self.assertEqual(len(ex), 1)
        e = ex[0]
        self.assertEqual((e["mt"], e["rfq"], e["px"], e["side"], e["n"]), (COMBO, "r1", "0.2500", "yes", 1))
        self.assertEqual(e["legs"], LEGS)
        self.assertEqual((e["tc"], e["cn"], e["cr"]), ("10.0000", None, "abcdef01"))
        self.assertEqual(e["ts_ms"] - e["rfq_ms"], 2500)

    def test_prints_outside_the_window_or_on_other_combos_do_not_match(self):
        lg, w, _ = self.make()
        lg.handle(rfq_msg("r1", COMBO, LEGS, T0))
        lg.handle(trade_msg(COMBO, T0 + cl.MATCH_WINDOW_S + 1, seq=1))
        lg.handle(trade_msg("KXMVECROSSCATEGORY-S2026OTHER-X", T0 + 1, seq=2))
        lg.handle(trade_msg("KXNBAGAME-26NOV05MIAGSW-GSW", T0 + 1, seq=3))   # a single, not a combo
        self.assertEqual(w.kinds("exec"), [])
        self.assertEqual((lg.st["unmatched"], lg.st["mve_prints"], lg.st["prints"]), (2, 2, 3))

    def test_contract_sized_rfqs_keep_their_contracts(self):
        lg, w, _ = self.make()
        lg.handle(rfq_msg("r1", COMBO, LEGS, T0, tc=None, cn="250.00"))
        lg.handle(trade_msg(COMBO, T0 + 1))
        self.assertEqual((w.kinds("exec")[0]["tc"], w.kinds("exec")[0]["cn"]), (None, "250.00"))

    def test_batch_children_get_their_own_legs(self):
        lg, w, _ = self.make()
        legs = [{"event_ticker": "E1", "market_ticker": "E1-A", "side": "yes"},
                {"event_ticker": "E2", "market_ticker": "E2-B", "side": "no"},
                {"event_ticker": "E3", "market_ticker": "E3-C", "side": "yes"}]
        lg.handle({"type": "rfq_batch_created", "sid": 1, "seq": 1,
                   "msg": {"id": "b1", "creator_id": "zz", "created_ts": iso(T0), "legs": legs,
                           "rfqs": [{"id": "c1", "market_ticker": "KXMVE-1", "target_cost_dollars": "5",
                                     "leg_indices": [0, 1]},
                                    {"id": "c2", "market_ticker": "KXMVE-2", "target_cost_dollars": "5",
                                     "leg_indices": [1, 2]}]}})
        self.assertEqual(lg.rfqs["KXMVE-1"]["legs"], [["E1-A", "yes"], ["E2-B", "no"]])
        self.assertEqual(lg.rfqs["KXMVE-2"]["legs"], [["E2-B", "no"], ["E3-C", "yes"]])
        self.assertEqual(lg.st["batch"], 1)

    def test_old_rfqs_are_forgotten_and_the_minute_stat_written(self):
        lg, w, clock = self.make()
        lg.handle(rfq_msg("r1", COMBO, LEGS, T0))
        lg.handle({"type": "rfq_deleted", "sid": 1, "seq": 2,
                   "msg": {"id": "r1", "market_ticker": COMBO, "deleted_ts": iso(T0 + 7)}})
        clock[0] = T0 + cl.RFQ_TTL_S + 5
        lg.upkeep(clock[0])
        self.assertEqual(len(lg.rfqs), 0)
        st = w.kinds("stat")
        self.assertEqual(len(st), 1)
        self.assertEqual((st[0]["rfq_combo"], st[0]["del"], st[0]["life_p50_ms"]), (1, 1, 7000))

    def test_seq_gaps_are_counted_per_subscription(self):
        lg, _, _ = self.make()
        lg.handle(rfq_msg("r1", COMBO, LEGS, T0, seq=1))
        lg.handle(rfq_msg("r2", COMBO + "2", LEGS, T0, seq=3))      # 2 missing
        lg.handle(trade_msg(COMBO, T0 + 1, seq=1))                   # its own sid
        self.assertEqual(lg.st["gaps"], 1)
        lg.reset_connection()
        lg.handle(rfq_msg("r3", COMBO + "3", LEGS, T0, seq=1))      # a new connection restarts
        self.assertEqual(lg.st["gaps"], 1)

    def test_sampling_is_deterministic_and_about_one_in_n(self):
        keys = [f"id-{i}" for i in range(20000)]
        picked = [k for k in keys if cl.sampled(k, 20)]
        self.assertEqual(picked, [k for k in keys if cl.sampled(k, 20)])
        self.assertTrue(800 < len(picked) < 1200)
        self.assertTrue(all(cl.sampled(k, 1) for k in keys[:10]))

    def test_subscribe_commands_shard_only_the_rfq_channel(self):
        cmds = cl.subscribe_cmds(50, 0)
        self.assertEqual(cmds[0]["params"], {"channels": ["communications"], "shard_factor": 50,
                                             "shard_key": 0})
        self.assertEqual(cmds[1]["params"], {"channels": ["trade"]})
        self.assertNotIn("shard_factor", cl.subscribe_cmds(1, 0)[0]["params"])

    def test_day_writer_rolls_at_utc_midnight_and_gzips_the_closed_day(self):
        with tempfile.TemporaryDirectory() as d:
            clock = [datetime(2026, 11, 5, 23, 59, 59, tzinfo=timezone.utc).timestamp()]
            w = cl.DayWriter(d, clock=lambda: clock[0])
            w.write({"k": "a"})
            clock[0] += 2
            w.write({"k": "b"})
            w.close()                         # waits for the roll's gzip
            old = os.path.join(d, "combo_2026-11-05.jsonl")
            self.assertEqual(len(w.gz_threads), 1)
            self.assertFalse(os.path.exists(old))
            with gzip.open(old + ".gz", "rt", encoding="utf-8") as f:
                self.assertEqual([json.loads(x)["k"] for x in f], ["a"])
            with open(os.path.join(d, "combo_2026-11-06.jsonl"), encoding="utf-8") as f:
                self.assertEqual([json.loads(x)["k"] for x in f], ["b"])

    def test_one_logger_at_a_time(self):
        with tempfile.TemporaryDirectory() as d:
            a = cl.single_instance(d)
            self.assertIsNotNone(a)
            self.assertIsNone(cl.single_instance(d))
            a.close()
            b = cl.single_instance(d)
            self.assertIsNotNone(b)
            b.close()

    def test_a_stop_file_keeps_it_from_starting(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, cl.STOP_NAME), "w").close()
            # 3 = the launcher loop stops (run_combo_rfq_logger.ps1)
            self.assertEqual(cl.run(d, 1.0, 50, 0, url="wss://unused.invalid"), cl.EXIT_STOP)
            self.assertEqual(sorted(os.listdir(d)), [cl.STOP_NAME])     # nothing opened, no lock

    def test_a_held_lock_exits_4_before_any_network(self):
        with tempfile.TemporaryDirectory() as d:
            held = cl.single_instance(d)
            try:
                # no exchange read, no socket: the 10-min task retries stay cheap
                self.assertEqual(cl.run(d, 1.0, 50, 0, url="wss://unused.invalid"), cl.EXIT_LOCKED)
            finally:
                held.close()


# ---- the scorer -------------------------------------------------------------------

def mkt(ticker, exp_s, event=None, **kw):
    m = {"ticker": ticker, "event_ticker": event or ticker.rsplit("-", 1)[0],
         "expected_expiration_time": iso(exp_s)}
    m.update(kw)
    return m


class FakeClient:
    def __init__(self, markets=None, candles=None):
        self.markets = markets or {}
        self.candle_rows = candles or {}
        self.calls = []

    def get(self, path, params):
        self.calls.append((path, dict(params)))
        if path == "/markets":
            return {"markets": [self.markets[t] for t in params["tickers"].split(",") if t in self.markets]}
        if path == "/markets/candlesticks":
            out = []
            for t in params["market_tickers"].split(","):
                rows = [{"end_period_ts": e, "yes_bid": {"close_dollars": f"{b:.4f}"},
                         "yes_ask": {"close_dollars": f"{a:.4f}"}}
                        for e, b, a in self.candle_rows.get(t, [])]
                out.append({"market_ticker": t, "candlesticks": rows})
            return {"markets": out}
        raise AssertionError(path)


def book_with(prices, end=T0 - 30):
    b = cs.PriceBook()
    b.add({t: [(int(end), bid, ask)] for t, (bid, ask) in prices.items()})
    return b


def exec_rec(legs, px="0.2500", ct="40.00", side="yes", tc="10.0000", tid="t1", rfq_ms=None, mt=COMBO):
    return {"k": "exec", "tid": tid, "mt": mt, "px": px, "ct": ct, "side": side, "legs": legs,
            "tc": tc, "cn": None if tc else ct, "rfq_ms": int((rfq_ms or T0) * 1000),
            "ts_ms": int((rfq_ms or T0) * 1000) + 2000}


class ScoreTests(unittest.TestCase):

    def setUp(self):
        self.markets = {"KXNBAGAME-26NOV05MIAGSW-GSW": mkt("KXNBAGAME-26NOV05MIAGSW-GSW", T0 + 6 * 3600),
                        "KXNHLGAME-26NOV05BOSTOR-BOS": mkt("KXNHLGAME-26NOV05BOSTOR-BOS", T0 + 5 * 3600),
                        "KXNBAPTS-26NOV05MIAGSW-GSWSCURRY30-25": mkt(
                            "KXNBAPTS-26NOV05MIAGSW-GSWSCURRY30-25", T0 + 9 * 3600,
                            event="KXNBAPTS-26NOV05MIAGSW"),
                        COMBO: {"ticker": COMBO, "result": "no", "status": "finalized"}}
        self.starts = cs.game_starts(self.markets)

    def test_grid_rounding_follows_the_combo_grid(self):
        self.assertEqual((cs.grid_up(0.0123), cs.grid_down(0.0123)), (0.013, 0.012))
        self.assertEqual((cs.grid_up(0.00567), cs.grid_down(0.00567)), (0.0057, 0.0056))
        self.assertEqual(cs.grid_up(0.25), 0.25)
        self.assertEqual(cs.grid_up(0.9951), 0.9951)

    def test_games_and_their_estimated_starts(self):
        self.assertEqual(cs.game_key("KXNBAPTS-26NOV05MIAGSW"), "26NOV05MIAGSW")
        # the game market's 6h expiry wins over the prop's 9h: start = 6h - 3h
        self.assertAlmostEqual(self.starts["26NOV05MIAGSW"], T0 + 3 * 3600)

    def test_an_eligible_execution_gets_the_product_of_leg_mids(self):
        book = book_with({LEGS[0][0]: (0.49, 0.51), LEGS[1][0]: (0.39, 0.41)})
        s = cs.score_exec(exec_rec(LEGS), self.markets, book, self.starts)
        self.assertIsNone(s["why"])
        self.assertAlmostEqual(s["fair"], 0.5 * 0.4)
        self.assertEqual((s["payoff"], s["bucket"], s["same_game"], s["in_play"]), (0.0, "2", False, False))
        # the makers sold YES at 25c on a combo that lost: they keep the premium
        self.assertAlmostEqual(s["mk_pnl"], 40 * 0.25)

    def test_no_legs_on_the_no_side_use_one_minus_the_mid(self):
        book = book_with({LEGS[0][0]: (0.49, 0.51), LEGS[1][0]: (0.39, 0.41)})
        s = cs.score_exec(exec_rec([LEGS[0], [LEGS[1][0], "no"]]), self.markets, book, self.starts)
        self.assertAlmostEqual(s["fair"], 0.5 * 0.6)

    def test_ineligible_reasons(self):
        prop = "KXNBAPTS-26NOV05MIAGSW-GSWSCURRY30-25"
        book = book_with({LEGS[0][0]: (0.49, 0.51), LEGS[1][0]: (0.30, 0.45), prop: (0.6, 0.62)})
        self.assertEqual(cs.score_exec(exec_rec(LEGS), self.markets, book, self.starts)["why"], "wide")
        book = book_with({LEGS[0][0]: (0.49, 0.51), LEGS[1][0]: (0.39, 0.41), prop: (0.6, 0.62)})
        sg = cs.score_exec(exec_rec([LEGS[0], [prop, "yes"]]), self.markets, book, self.starts)
        self.assertEqual((sg["why"], sg["same_game"]), ("same_game", True))
        late = cs.score_exec(exec_rec(LEGS, rfq_ms=T0 + 4 * 3600), self.markets,
                             book_with({LEGS[0][0]: (0.49, 0.51), LEGS[1][0]: (0.39, 0.41)}, end=T0 + 4 * 3600 - 30),
                             self.starts)
        self.assertEqual((late["why"], late["in_play"]), ("in_play", True))
        none = cs.score_exec(exec_rec(LEGS), self.markets, book_with({LEGS[0][0]: (0.49, 0.51)}), self.starts)
        self.assertEqual(none["why"], "no_price")
        # a one-sided leg (no bid) has no mid
        book = book_with({LEGS[0][0]: (0.0, 0.51), LEGS[1][0]: (0.39, 0.41)})
        self.assertEqual(cs.score_exec(exec_rec(LEGS), self.markets, book, self.starts)["why"], "no_price")

    def test_the_maker_fee_matches_kalshis_combo_row(self):
        # kalshi.com/fee-schedule (11/05): combo makers $0.04-$0.88 per 100 contracts
        # (50% of the taker's $0.07-$1.75), from a 1c to a 50c price
        self.assertEqual(round(cs.maker_fee(100, 0.01), 2), 0.03)      # 0.03465: shown as $0.04
        self.assertAlmostEqual(cs.maker_fee(100, 0.01), 0.03465)
        self.assertAlmostEqual(cs.maker_fee(100, 0.50), 0.875)          # shown as $0.88
        self.assertAlmostEqual(cs.maker_fee(100, 0.50), 0.5 * 0.07 * 100 * 0.25)

    def test_all_nfl_cross_game_combos_are_flagged_for_the_exemption(self):
        a, b = "KXNFLGAME-26NOV08KCBUF-KC", "KXNFLGAME-26NOV08DALPHI-PHI"
        prop = "KXNFLPASSYDS-26NOV08KCBUF-KCPMAHOMES15-250"
        mk = {t: mkt(t, T0 + 30 * 3600) for t in (a, b, prop)}
        for m in mk.values():
            m.pop("event_ticker")           # no event in the read: the ticker's game segment decides
        mk[COMBO] = {"ticker": COMBO, "result": "", "status": "active"}
        book = book_with({a: (0.59, 0.61), b: (0.44, 0.46), prop: (0.49, 0.51)})
        st = cs.game_starts(mk)
        nfl = cs.score_exec(exec_rec([[a, "yes"], [b, "yes"]], px="0.3500"), mk, book, st)  # fair 0.27
        self.assertTrue(nfl["nfl_uncorr"])
        self.assertIsNone(nfl["why"])
        sgp = cs.score_exec(exec_rec([[a, "yes"], [prop, "yes"]]), mk, book, st)
        self.assertFalse(sgp["nfl_uncorr"])                 # same game: correlated
        mixed = cs.score_exec(exec_rec([[a, "yes"], LEGS[0]]), dict(mk, **self.markets),
                              book_with({a: (0.59, 0.61), LEGS[0][0]: (0.49, 0.51)}), st)
        self.assertFalse(mixed["nfl_uncorr"])               # not all NFL
        rep = cs.build([dict(nfl, payoff=0.0, mk_pnl=10.0)])
        won = [r for r in rep["shadow"] if r["wins"]]
        self.assertTrue(won and all(abs(r["fee_nfl"] - r["fee"]) < 1e-12 for r in won))

    def test_strategy_1_takes_only_the_pilots_combos(self):
        # 3 main-line legs on 3 games, fair 0.6 x 0.5 x 0.7 = 0.21, legs 2c wide
        nhl2 = "KXNHLGAME-26NOV05NYRPIT-NYR"
        mk = dict(self.markets, **{nhl2: mkt(nhl2, T0 + 6 * 3600)})
        legs = [LEGS[0], LEGS[1], [nhl2, "no"]]
        book = book_with({LEGS[0][0]: (0.59, 0.61), LEGS[1][0]: (0.49, 0.51), nhl2: (0.29, 0.31)})
        st = cs.game_starts(mk)
        s = cs.score_exec(exec_rec(legs, px="0.2500"), mk, book, st)
        self.assertTrue(s["main_only"])
        self.assertAlmostEqual(s["fair"], 0.21)
        self.assertTrue(cs.is_s1(s))
        self.assertFalse(cs.is_s1(cs.score_exec(exec_rec(LEGS, px="0.2500"), mk, book, st)))   # 2 legs
        prop = "KXNBAPTS-26NOV05MIAGSW-GSWSCURRY30-25"
        book2 = book_with({LEGS[1][0]: (0.49, 0.51), nhl2: (0.29, 0.31), prop: (0.59, 0.61)})
        self.assertFalse(cs.is_s1(cs.score_exec(exec_rec([[prop, "yes"], LEGS[1], [nhl2, "no"]]),
                                                mk, book2, st)))                             # a prop
        rep = cs.build([s])
        self.assertEqual(rep["s1"]["eligible"], 1)
        r10 = next(r for r in rep["s1"]["rows"] if r["markup"] == 0.10)
        self.assertEqual(r10["wins"], 1)                    # 0.231 < the 0.25 print
        self.assertGreater(r10["net"], 0)                   # the combo lost (payoff 0)

    def test_the_candle_used_ended_before_the_rfq(self):
        b = cs.PriceBook()
        b.add({"X": [(int(T0 - 60), 0.40, 0.42), (int(T0 + 1), 0.70, 0.72)]})
        self.assertEqual(b.price_at("X", T0)[:2], (0.40, 0.42))      # never the minute after
        self.assertIsNone(b.price_at("X", T0 - 120))
        self.assertIsNone(b.price_at("Y", T0))

    def test_shadow_fill_on_a_yes_buyer(self):
        s = {"why": None, "fair": 0.20, "side": "yes", "px": 0.25, "ct": 40.0, "tc": True,
             "payoff": 0.0, "grid": cs.DEFAULT_GRID}
        f = cs.shadow_fill(s, 0.10)                     # ask 0.22 < 0.25: we win
        self.assertAlmostEqual(f["price"], 0.22)
        self.assertAlmostEqual(f["cts"], 40 * 0.25 / 0.22)  # the same $10 at our price
        self.assertAlmostEqual(f["pnl"], f["cts"] * 0.22)
        self.assertAlmostEqual(f["fee"], 0.035 * f["cts"] * 0.22 * 0.78)
        self.assertIsNone(cs.shadow_fill(dict(s, px=0.22), 0.10))     # a tie is not a win
        self.assertIsNone(cs.shadow_fill(dict(s, why="same_game"), 0.10))
        lost = cs.shadow_fill(dict(s, payoff=1.0, tc=False), 0.10)
        self.assertAlmostEqual(lost["pnl"], 40 * (0.22 - 1.0))
        self.assertIsNone(cs.shadow_fill(dict(s, payoff=None), 0.10)["pnl"])

    def test_shadow_fill_on_a_yes_seller(self):
        s = {"why": None, "fair": 0.20, "side": "no", "px": 0.15, "ct": 40.0, "tc": False,
             "payoff": 1.0, "grid": cs.DEFAULT_GRID}
        f = cs.shadow_fill(s, 0.10)                     # bid 0.18 > 0.15: we win
        self.assertAlmostEqual(f["price"], 0.18)
        self.assertAlmostEqual(f["pnl"], 40 * (1.0 - 0.18))
        self.assertIsNone(cs.shadow_fill(dict(s, px=0.19), 0.10))

    def test_build_adds_up_the_flow_and_the_shadow(self):
        book = book_with({LEGS[0][0]: (0.49, 0.51), LEGS[1][0]: (0.39, 0.41)})
        rows = [cs.score_exec(exec_rec(LEGS, px="0.2500", tid=f"t{i}"), self.markets, book, self.starts)
                for i in range(3)]
        rep = cs.build(rows)
        self.assertEqual((rep["n_scored"], rep["n_settled"], rep["n_eligible"]), (3, 3, 3))
        self.assertAlmostEqual(rep["flow"]["gross"], 3 * 40 * 0.25)
        self.assertAlmostEqual(rep["flow"]["hold"], 1.0)               # every combo lost
        sh = {r["markup"]: r for r in rep["shadow"]}
        self.assertEqual(sh[0.10]["wins"], 3)                          # 22c beats the 25c print
        self.assertEqual(sh[0.30]["wins"], 0)                          # 26c does not
        self.assertGreater(sh[0.10]["net"], 0)
        self.assertEqual(rep["markup"]["2"]["n"], 3)
        self.assertAlmostEqual(rep["markup"]["2"]["median"], 0.25)
        text = cs.render(rep, cs.flow_context([], []), "2026-11-05", "2026-11-05")
        self.assertIn("The shadow maker", text)
        pending = cs.build([dict(r, payoff=None, mk_pnl=None) for r in rows])
        text = cs.render(pending, cs.flow_context([], []), "2026-11-05", "2026-11-05")
        self.assertIn("No scored combo has settled yet", text)
        self.assertNotIn("The shadow maker", text)

    def test_load_reads_plain_and_gzip_days_and_skips_a_torn_line(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "combo_2026-11-05.jsonl"), "w", encoding="utf-8") as f:
                f.write(json.dumps(exec_rec(LEGS, tid="a")) + "\n")
                f.write(json.dumps(exec_rec(LEGS, tid="a")) + "\n")        # a duplicate
                f.write(json.dumps({"k": "stat", "secs": 60, "rfq": 10, "rfq_combo": 9, "matched": 1,
                                    "mve_prints": 40, "mve_usd": 100.0, "shard": [50, 0]}) + "\n")
                f.write('{"k": "exec", "tid": "torn')
            with gzip.open(os.path.join(d, "combo_2026-11-04.jsonl.gz"), "wt", encoding="utf-8") as f:
                f.write(json.dumps(exec_rec(LEGS, tid="b")) + "\n")
            execs, _rfqs, stats, _runs = cs.load(d, datetime(2026, 11, 4).date(), datetime(2026, 11, 5).date())
            self.assertEqual(sorted(e["tid"] for e in execs), ["a", "b"])
            ctx = cs.flow_context(stats, [])
            self.assertAlmostEqual(ctx["rfq_per_s_full"], 10 * 50 / 60)
            self.assertAlmostEqual(ctx["exec_share"], 1 / 9)

    def test_pick_caps_each_day_by_trade_id(self):
        rows = [exec_rec(LEGS, tid=f"t{i}") for i in range(10)]
        got = cs.pick(rows, per_day=4)
        self.assertEqual(len(got), 4)
        self.assertEqual([r["tid"] for r in got], [r["tid"] for r in cs.pick(list(reversed(rows)), per_day=4)])
        self.assertEqual(got, sorted(got, key=lambda e: zlib.crc32(e["tid"].encode()))[:4])

    def test_fetch_batches_markets_and_candles(self):
        legs = [[f"KXNBAGAME-26NOV05G{i:03d}-A", "yes"] for i in range(150)]
        markets = {l[0]: mkt(l[0], T0 + 6 * 3600) for l in legs}
        candles = {l[0]: [(int(T0 - 60), 0.5, 0.52)] for l in legs}
        fc = FakeClient(markets, candles)
        r = cs.Reader(fc, gap_s=0.0)
        mk, book = cs.fetch(r, [exec_rec(legs[:75], tid="a"), exec_rec(legs[75:], tid="b")])
        self.assertEqual(len(mk), 150)
        self.assertEqual(sum(1 for p, _ in fc.calls if p == "/markets"), 2)                # 151 tickers / 100
        self.assertEqual(sum(1 for p, _ in fc.calls if p == "/markets/candlesticks"), 2)   # one hour, 150 legs
        self.assertEqual(book.price_at(legs[149][0], T0)[:2], (0.5, 0.52))


if __name__ == "__main__":
    unittest.main()
