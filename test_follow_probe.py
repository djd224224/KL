"""Tests for follow_probe.py. Every test writes only to its own temp dir."""

import json
import os
import random
import tempfile
import time
import unittest

import follow_probe as fp


def cfg(**kw):
    base = dict(pre_s=10.0, hold_s=100.0, post_s=50.0, debounce_s=1.0, book_wait_s=20.0,
                max_open=2, scan_max_age_s=10 ** 9, reconcile_s=10 ** 9, tick_s=0.5)
    base.update(kw)
    return fp.Config(**base).validate()


def cand(t, ev, bid=30.0, ask=45.0, vol=0.0):
    return fp.Cand(ticker=t, event=ev, bid_c=bid, ask_c=ask, vol24h=vol,
                   prog={"reward_day": 10.0, "target": 1000.0})


def book(yes=None, no=None):
    """{cents: size} YES bids / NO bids, as Trial.observe takes them."""
    return dict(yes or {}), dict(no or {})


class FakeFeed:
    def __init__(self):
        self.books = {}          # ticker -> (yes {c: q}, no {c: q}) or None
        self.markets = set()
        self.events = []
        self.dirty = set()
        self.event_flag = None

    def set(self, t, yes=None, no=None):
        self.books[t] = book(yes, no)
        self.dirty.add(t)

    def book_levels(self, t):
        b = self.books.get(t)
        if b is None or t not in self.markets:
            return None
        return ({p / 100.0: q for p, q in b[0].items()}, {p / 100.0: q for p, q in b[1].items()})

    def set_markets(self, ts):
        self.markets = set(ts)

    def drain(self):
        d, e = self.dirty, self.events
        self.dirty, self.events = set(), []
        return d, e, 0

    def status(self):
        return {"fake": True}


class FakeEx:
    def __init__(self, live=False, sub=0, booked=None):
        self.live, self.sub = live, sub
        self.created, self.cancelled = [], []
        self.primary = {}
        self.booked = booked
        self.resting, self.positions, self.cash = [], [], 50.0
        self.n = 0

    def primary_resting(self, ticker=None):
        return self.primary.get(ticker, [])

    def create(self, ticker, side, x, coid, exp):
        if not self.live:
            raise AssertionError("dry run must never place")
        self.n += 1
        self.created.append((ticker, side, x, coid, exp))
        return f"oid{self.n}"

    def cancel(self, oid, ticker, subaccount=None):
        self.cancelled.append((oid, ticker, subaccount))
        return "ok"

    def order_subaccount(self, oid, tries=2):
        return self.sub if self.booked is None else self.booked

    def sub_resting(self, ticker=None):
        return [o for o in self.resting if ticker is None or o.get("ticker") == ticker]

    def sub_positions(self):
        return self.positions

    def balance(self):
        return self.cash

    def sub_fills(self, min_ts):
        return []


class FakeScanner:
    def __init__(self, cands, ts):
        self.res = fp.ScanResult(ts=ts, cands=cands, excl_events=set(), excl_markets=set(),
                                 stats={})

    def latest(self):
        return self.res


def rows_of(d, name="trials"):
    out = []
    for n in sorted(os.listdir(d)):
        if n.startswith(name + "_"):
            with open(os.path.join(d, n), encoding="utf-8") as f:
                out += [json.loads(line) for line in f]
    return out


class TestConfig(unittest.TestCase):
    def test_env(self):
        c = fp.Config.from_env({"FPROBE_HOLD_S": "300", "FPROBE_MAX_OPEN": "3",
                                "FPROBE_SIDE": "ask", "FPROBE_SEED": "9"})
        self.assertEqual((c.hold_s, c.max_open, c.side, c.seed), (300.0, 3, "ask", 9))
        self.assertIsInstance(c.max_open, int)

    def test_bad(self):
        for env in ({"FPROBE_SIDE": "both"}, {"FPROBE_IMM_EXCLUDE": "x"},
                    {"FPROBE_MIN_SPREAD_C": "1"}, {"FPROBE_HOLD_S": "0"},
                    {"FPROBE_MAX_SPREAD_C": "10"}):
            with self.assertRaises(ValueError):
                fp.Config.from_env(env)


class TestPricing(unittest.TestCase):
    def test_bid_floor_mid(self):
        own, opp = fp.own_view({30: 5}, {55: 7}, "bid")     # YES 30 / 45
        self.assertEqual((fp.best(own), opp), (30, 45.0))
        self.assertEqual(fp.theo_x(30, 45.0, cfg()), 37.0)
        self.assertEqual(fp.yes_price_c("bid", 37.0), 37.0)

    def test_ask_ceil_mid(self):
        own, opp = fp.own_view({30: 5}, {55: 7}, "ask")     # sell YES: NO-bid space
        self.assertEqual((fp.best(own), opp), (55, 70.0))
        x = fp.theo_x(55, 70.0, cfg())
        self.assertEqual(x, 62.0)
        self.assertEqual(fp.yes_price_c("ask", x), 38.0)     # ceil(37.5)

    def test_edge_and_limits(self):
        self.assertEqual(fp.theo_x(30, 45.0, cfg(edge_c=3)), 34.0)
        self.assertIsNone(fp.theo_x(30, 31.0, cfg()))        # no room inside
        self.assertIsNone(fp.theo_x(None, 45.0, cfg()))
        self.assertIsNone(fp.theo_x(30, 100.0, cfg()))       # one-sided
        self.assertIsNone(fp.theo_x(30, 45.0, cfg(edge_c=8)))  # backed off to the touch

    def test_subpenny_levels(self):
        self.assertEqual(fp.levels_cents({0.305: 2.0, 0.31: 0.0}), {30.5: 2.0})
        self.assertEqual(fp.theo_x(30.5, 45.0, cfg()), 37.0)


def mrow(t, ev, bid="0.3000", ask="0.4500", **kw):
    m = {"ticker": t, "event_ticker": ev, "status": "active", "exchange_index": 0,
         "yes_bid_dollars": bid, "yes_ask_dollars": ask, "close_time": "2030-01-01T00:00:00Z",
         "volume_24h_fp": "0.00"}
    m.update(kw)
    return m


def prog(t, end="2030-01-01T00:00:00Z"):
    return {"market_ticker": t, "incentive_type": "liquidity", "period_reward": 700000,
            "start_date": "2029-12-25T00:00:00Z", "end_date": end, "target_size_fp": "500.00",
            "discount_factor_bps": 5000}


class TestUniverse(unittest.TestCase):
    NOW = 1893000000.0          # 2029-12-26

    def test_reasons(self):
        c = cfg()
        p = {"end_ts": self.NOW + 86400 * 5}
        self.assertIsNone(fp.market_reason(mrow("A", "E"), p, self.NOW, c))
        self.assertEqual(fp.market_reason(mrow("A", "E", ask="0.3900"), p, self.NOW, c), "tight")
        self.assertEqual(fp.market_reason(mrow("A", "E", bid="0.0900", ask="0.9500"), p,
                                          self.NOW, c), "stub")
        self.assertIsNone(fp.market_reason(mrow("A", "E", bid="0.0900", ask="0.9500"), p,
                                           self.NOW, cfg(max_spread_c=0)))
        self.assertEqual(fp.market_reason(mrow("A", "E", bid="0.0000"), p, self.NOW, c),
                         "one_sided")
        self.assertEqual(fp.market_reason(mrow("A", "E", exchange_index=3), p, self.NOW, c),
                         "shard")
        self.assertEqual(fp.market_reason(mrow("A", "E", close_time="2029-12-26T01:00:00Z"),
                                          p, self.NOW, c), "closing")
        self.assertEqual(fp.market_reason(mrow("A", "E"), {"end_ts": self.NOW + 100},
                                          self.NOW, c), "program_ending")
        self.assertEqual(fp.market_reason(mrow("KXTEMPMIAH-X", "KXTEMPMIAH-26"), p,
                                          self.NOW, c), "series")

    def test_build_scan_excludes_primary(self):
        progs = [prog("A1"), prog("A2"), prog("B1"), prog("C1")]
        ms = [mrow("A1", "EA"), mrow("A2", "EA"), mrow("B1", "EB", volume_24h_fp="12.00"),
              mrow("C1", "EC"), mrow("D1", "ED")]                     # D1: no program
        res = fp.build_scan(progs, ms, [{"ticker": "A1"}], self.NOW, cfg())
        self.assertEqual(sorted(c.ticker for c in res.cands), ["B1", "C1"])
        self.assertEqual(res.stats["skipped"], {"imm_market": 1, "imm_event": 1})
        self.assertEqual(res.cands[0].prog["reward_day"], 10.0)        # $70 over 7 days
        self.assertEqual({c.ticker: c.stratum for c in res.cands}, {"B1": "active", "C1": "quiet"})
        res2 = fp.build_scan(progs, ms, [{"ticker": "A1"}], self.NOW, cfg(imm_exclude="market"))
        self.assertEqual(sorted(c.ticker for c in res2.cands), ["A2", "B1", "C1"])


class TestPickProbes(unittest.TestCase):
    def test_probes(self):
        cands = [cand(f"Q{i}-{j}", f"Q{i}") for i in range(4) for j in range(3)]
        cands += [cand(f"A{i}", f"EA{i}", vol=5) for i in range(2)]
        out = fp.pick_probes(cands, 10, set(), set(), set(), cfg(), random.Random(1))
        self.assertEqual(len(out), 6)                          # 4 quiet + 2 active events
        evs = [c.event for c, _, _ in out]
        self.assertEqual(len(evs), len(set(evs)))              # one market per event
        for c, side, s in out:
            self.assertEqual(c.stratum, s)
            self.assertIn(side, ("bid", "ask"))
        again = fp.pick_probes(cands, 10, set(), set(), set(), cfg(), random.Random(1))
        self.assertEqual([(c.ticker, s) for c, s, _ in out], [(c.ticker, s) for c, s, _ in again])
        self.assertEqual(len(fp.pick_probes(cands, 2, set(), set(), set(), cfg(),
                                            random.Random(1))), 2)

    def test_filters_and_side(self):
        cands = [cand("A", "EA"), cand("B", "EB"), cand("C", "EC"), cand("D", "ED")]
        out = fp.pick_probes(cands, 5, {"EA"}, {"B"}, {"EC"}, cfg(side="ask"), random.Random(2))
        self.assertEqual([(c.ticker, side) for c, side, _ in out], [("D", "ask")])

    def test_balanced_strata(self):
        cands = [cand(f"Q{i}", f"EQ{i}") for i in range(40)]
        cands += [cand(f"A{i}", f"EA{i}", vol=5) for i in range(40)]
        n = {"active": 0, "quiet": 0}
        for seed in range(200):
            for _, _, s in fp.pick_probes(cands, 6, set(), set(), set(), cfg(), random.Random(seed)):
                n[s] += 1
        self.assertLess(abs(n["active"] - n["quiet"]), 150)    # of 1,200


class TestTrialMetrics(unittest.TestCase):
    def mk(self, arm="dry", side="bid", **kw):
        c = cfg(**kw)
        tr = fp.Trial("t1", cand("M", "EM"), side, "quiet", 0.0, c, arm == "probe")
        tr.phase, tr.t_pre = "pre", 0.0
        tr.observe(0.0, book({30: 5}, {55: 7}))
        own, opp = fp.own_view({30: 5}, {55: 7}, side)
        tr.start_hold(10.0, own, opp, fp.theo_x(fp.best(own), opp, c))
        tr.observe(10.0, book({30: 5}, {55: 7}))
        return tr

    def test_join_debounced(self):
        tr = self.mk()
        self.assertEqual((tr.x, tr.b0, tr.a0), (37.0, 30, 45.0))
        for t in (15.0, 15.5, 16.1):
            tr.observe(t, book({30: 5, 37: 50}, {55: 7}))
        self.assertEqual(tr.ev.get("join"), 5.0)
        self.assertEqual(tr.ev.get("follow"), 5.0)
        self.assertEqual(tr.ev.get("step"), 5.0)
        self.assertNotIn("improve", tr.ev)
        self.assertEqual(tr.follow_size_max, 50.0)
        self.assertEqual(tr.row("dry", "r")["gap_closed"], 1.0)

    def test_flicker_is_raw_only(self):
        tr = self.mk()
        tr.observe(20.0, book({30: 5, 38: 10}, {55: 7}))
        tr.observe(20.4, book({30: 5}, {55: 7}))
        tr.observe(25.0, book({30: 5}, {55: 7}))
        self.assertEqual(tr.raw.get("improve"), 10.0)
        self.assertNotIn("improve", tr.ev)
        self.assertNotIn("follow", tr.ev)

    def test_improve_and_partial_step(self):
        tr = self.mk()
        tr.observe(12.0, book({34: 3, 30: 5}, {55: 7}))
        tr.observe(13.5, book({34: 3, 30: 5}, {55: 7}))
        self.assertEqual(tr.ev.get("step"), 2.0)
        self.assertNotIn("follow", tr.ev)
        tr.observe(30.0, book({39: 1, 34: 3}, {55: 7}))
        tr.observe(32.0, book({39: 1, 34: 3}, {55: 7}))
        self.assertEqual(tr.ev.get("improve"), 20.0)
        self.assertEqual(tr.improve_by_max, 2.0)
        self.assertEqual(tr.row("dry", "r")["gap_closed"], round(9 / 7, 3))

    def test_probe_excludes_our_contract(self):
        tr = self.mk(arm="probe")
        tr.our_rem = 1.0
        for t in (12.0, 14.0):
            tr.observe(t, book({30: 5, 37: 1}, {55: 7}))        # only ours
        self.assertNotIn("join", tr.ev)
        for t in (20.0, 22.0):
            tr.observe(t, book({30: 5, 37: 3}, {55: 7}))        # 2 more joined
        self.assertEqual(tr.ev.get("join"), 10.0)
        self.assertEqual(tr.follow_size_max, 2.0)

    def test_fill_race_not_a_join(self):
        tr = self.mk(arm="probe")
        tr.our_rem = 1.0
        tr.observe(12.0, book({30: 5, 37: 1}, {55: 7}))
        tr.our_rem = 0.0                                        # the fill came first
        tr.observe(12.1, book({30: 5, 37: 1}, {55: 7}))
        tr.observe(12.3, book({30: 5}, {55: 7}))                # then its delta
        tr.observe(20.0, book({30: 5}, {55: 7}))
        self.assertNotIn("join", tr.ev)

    def test_opp_side(self):
        tr = self.mk()
        tr.observe(40.0, book({30: 5}, {58: 4}))                # YES ask 45 -> 42
        tr.observe(42.0, book({30: 5}, {58: 4}))
        self.assertEqual(tr.ev.get("opp_step"), 30.0)
        self.assertEqual(tr.row("dry", "r")["opp_gap_closed"], round(3 / 8, 3))

    def test_ask_side_join(self):
        tr = self.mk(side="ask")
        self.assertEqual((tr.x, tr.b0, tr.a0), (62.0, 55, 70.0))
        for t in (15.0, 17.0):
            tr.observe(t, book({30: 5}, {55: 7, 62: 20}))      # a YES ask at 38
        self.assertEqual(tr.ev.get("join"), 5.0)

    def test_post_gone_and_revert(self):
        tr = self.mk()
        for t in (15.0, 17.0):
            tr.observe(t, book({30: 5, 37: 50}, {55: 7}))
        tr.start_post(110.0)
        self.assertEqual((tr.t1_best, tr.t1_size), (37, 50.0))
        tr.observe(110.0, book({30: 5, 37: 50}, {55: 7}))
        tr.observe(130.0, book({30: 5}, {55: 7}))
        tr.observe(131.5, book({30: 5}, {55: 7}))
        r = tr.row("dry", "r")
        self.assertEqual(r["gone_s"], 20.0)
        self.assertEqual(r["revert"], 1.0)

    def test_unavailable_time(self):
        tr = self.mk()
        tr.observe(20.0, None)
        tr.observe(30.0, None)
        tr.observe(35.0, book({30: 5}, {55: 7}))
        self.assertAlmostEqual(tr.unavail_s, 15.0)


class BotHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.feed = FakeFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def bot(self, live=False, cands=None, booked=None, **kw):
        c = cfg(**kw)
        self.ex = FakeEx(live=live, sub=7 if live else 0, booked=booked)
        cands = cands or [cand("A", "EA"), cand("B", "EB")]
        for x in cands:
            self.feed.set(x.ticker, {30: 5}, {55: 7})
        b = fp.Bot(c, self.ex, self.feed, fp.Log(self.dir, echo=False), self.dir, live,
                   random.Random(3), FakeScanner(cands, 0.0), run_id="R")
        return b

    def run_to(self, b, t0, t1, dt=0.5):
        t = t0
        while t <= t1 + 1e-9:
            b.step(t)
            t += dt


class TestBotFlow(BotHarness):
    def test_dry_probes_run_to_rows_without_orders(self):
        b = self.bot()
        self.run_to(b, 0.0, 200.0)
        rows = rows_of(self.dir)
        self.assertEqual(sorted(r["ticker"] for r in rows), ["A", "B"])
        self.assertTrue(all(r["status"] == "done" and r["mode"] == "dry" for r in rows))
        self.assertTrue(all(r["x"] in (37.0, 62.0) for r in rows))
        self.assertTrue(all("pair" not in r and r["arm"] == "probe" for r in rows))
        self.assertEqual(self.ex.created, [])
        self.assertEqual(b.counts["probes"], 2)
        self.assertEqual(self.feed.markets, set())              # unsubscribed at the end

    def test_live_places_every_probe(self):
        b = self.bot(live=True)
        self.run_to(b, 0.0, 20.0)
        self.assertEqual(sorted(c[0] for c in self.ex.created), ["A", "B"])

    def test_live_places_fills_and_cancels(self):
        b = self.bot(live=True, side="bid", max_open=1)
        self.run_to(b, 0.0, 20.0)
        self.assertEqual(len(self.ex.created), 1)
        t, side, x, coid, exp = self.ex.created[0]
        self.assertEqual((side, x), ("bid", 37.0))
        self.assertTrue(coid.startswith("fpb-R-"))
        probe = b.by_oid["oid1"]
        self.assertEqual(probe.our_rem, 1.0)
        self.assertAlmostEqual(exp - probe.t0, 100 + 120, delta=1)
        self.feed.set(t, {30: 5, 37: 1}, {55: 7})             # our contract shows
        self.run_to(b, 20.5, 60.0)
        self.assertNotIn("join", probe.ev)
        self.run_to(b, 60.5, 175.0)
        self.assertEqual([c[0] for c in self.ex.cancelled], ["oid1"])
        rows = rows_of(self.dir)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["placed"])
        self.assertEqual(rows[0]["order_id"], "oid1")
        self.assertEqual(rows_of(self.dir, "orders")[0]["event"], "placed")

    def test_live_fill_stops_the_cancel(self):
        b = self.bot(live=True, side="bid", max_open=1)
        self.run_to(b, 0.0, 20.0)
        t = self.ex.created[0][0]
        self.feed.events.append(("fill", {"order_id": "oid1", "trade_id": "x1",
                                          "count_fp": "1.00", "yes_price_dollars": "0.3700",
                                          "market_ticker": t}))
        self.run_to(b, 20.5, 175.0)
        self.assertEqual(self.ex.cancelled, [])
        self.assertEqual(b.counts["fills"], 1.0)
        self.assertAlmostEqual(b.held_cost, 0.37)
        probe = rows_of(self.dir)[0]
        self.assertEqual((probe["fill_n"], probe["fill_px"]), (1.0, 37.0))

    def test_own_key_in_pre_abandons(self):
        b = self.bot()
        self.run_to(b, 0.0, 3.0)
        tr = b.by_ticker["A"]
        self.feed.events.append(("own_delta", {"market_ticker": "A", "client_order_id": "imm-9",
                                               "delta_fp": "5.00", "subaccount": 0}))
        self.run_to(b, 3.5, 200.0)
        rows = {r["ticker"]: r for r in rows_of(self.dir)}
        self.assertEqual(rows["A"]["status"], "abandoned")
        self.assertEqual(rows["A"]["reason"], "own_key_pre")
        self.assertEqual(rows["A"]["own_key_prefixes"], ["imm"])
        self.assertEqual(rows["B"]["status"], "done")
        self.assertIsNotNone(tr)

    def test_primary_resting_skips_the_market(self):
        b = self.bot()
        self.ex.primary["A"] = [{"ticker": "A"}]
        b.step(0.0)
        self.assertEqual([t.ticker for t in b.trials.values()], ["B"])
        self.assertIn("A", b.cooled)

    def test_wrong_subaccount_halts(self):
        b = self.bot(live=True, booked=0, max_open=1)
        self.run_to(b, 0.0, 20.0)
        self.assertTrue(b.halted)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "HALT")))
        self.assertIn(("oid1", self.ex.created[0][0], 0), self.ex.cancelled)
        self.assertEqual(b.trials, {})

    def test_halt_file_idles_and_resumes(self):
        b = self.bot(cands=[cand(f"T{i}", f"E{i}") for i in range(3)])
        b.step(0.0)
        self.assertEqual(b.open_count(), 2)
        open(os.path.join(self.dir, "HALT"), "w").write("stop\n")
        b.step(3.0)
        self.assertTrue(b.halted)
        self.assertEqual(b.trials, {})
        b.step(10.0)
        self.assertEqual(b.trials, {})
        os.remove(os.path.join(self.dir, "HALT"))
        b.step(13.0)
        b.step(20.0)
        self.assertFalse(b.halted)
        self.assertEqual(b.open_count(), 1)                    # 1 market left uncooled

    def test_caps(self):
        b = self.bot(live=True, max_probes_day=1)
        self.assertIsNone(b.caps_block())
        b.held_cost = 39.5
        self.assertIn("held", b.caps_block())
        b.held_cost = 0
        b.counts["probes"] = 1
        self.assertIn("probes today", b.caps_block())
        self.assertEqual(fp.held_cost_of([{"market_exposure_dollars": "0.37"},
                                          {"position_fp": "-2.00"}]), 2.37)


    def test_start_burst_ramps_up(self):
        cs = [cand(f"T{i}", f"E{i}") for i in range(10)]
        b = self.bot(cands=cs, max_open=10, start_burst=3)
        b.step(0.0)
        self.assertEqual(b.open_count(), 3)
        b.step(2.0)
        self.assertEqual(b.open_count(), 3)                    # the next start tick is 5 s on
        b.step(5.0)
        self.assertEqual(b.open_count(), 6)
        picks = rows_of(self.dir, "picks")
        self.assertEqual(len(picks), 6)
        self.assertTrue(all(p["mode"] == "dry" and p["event_ticker"] == "E" + p["ticker"][1:]
                            for p in picks))
        with self.assertRaises(ValueError):
            cfg(start_burst=0)


class TestRestore(BotHarness):
    def write(self, name, day, rows):
        with open(os.path.join(self.dir, f"{name}_{day}.jsonl"), "a", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_restart_keeps_caps_and_cooldowns(self):
        now = fp.parse_iso("2026-11-08T21:00:00Z")
        today, yday = "2026-11-08", "2026-11-07"
        self.write("orders", today, [               # a v2 run: no picks rows, no event_ticker
            {"ts": "2026-11-08T20:41:42Z", "tid": "r1-1", "event": "placed", "ticker": "KXA-26NOV09-T1"},
            {"ts": "2026-11-08T20:41:43Z", "tid": "r1-2", "event": "placed", "ticker": "KXB-30ARK-Y"},
            {"ts": "2026-11-08T20:51:43Z", "tid": "r1-1", "event": "cancel_ok",
             "ticker": "KXA-26NOV09-T1"}])
        fill = {"ts": "2026-11-08T20:41:47Z", "order_id": "o2", "tid": "r1-2",
                "body": {"trade_id": "t1", "order_id": "o2", "count_fp": "1.00"}}
        self.write("fills", today, [fill, fill])       # the same trade twice
        self.write("picks", today, [
            {"ts": "2026-11-08T20:58:00Z", "tid": "r2-1", "mode": "live", "ticker": "KXC-X",
             "event_ticker": "EVC"},
            {"ts": "2026-11-08T20:58:00Z", "tid": "d-1", "mode": "dry", "ticker": "KXD-X",
             "event_ticker": "EVD"}])
        self.write("trials", today, [
            {"ts": "2026-11-08T20:56:43Z", "tid": "r1-1", "mode": "live", "status": "done",
             "ticker": "KXA-26NOV09-T1", "event": "KXA-26NOV09",
             "ev": {"follow": 12.0, "join": 12.0}}])
        self.write("picks", yday, [
            {"ts": "2026-11-07T20:00:00Z", "tid": "y-1", "mode": "live", "ticker": "KXOLD-1",
             "event_ticker": "EVOLD"},                  # over 24 h ago: free again
            {"ts": "2026-11-07T22:00:00Z", "tid": "y-2", "mode": "live", "ticker": "KXRECENT-1",
             "event_ticker": "EVR"}])
        b = self.bot(live=True)
        msg = b.restore(now)
        self.assertEqual((b.counts["probes"], b.counts["placed"], b.counts["fills"]), (3, 2, 1.0))
        self.assertEqual((b.counts["done"], b.counts["followed"], b.counts["joined"]), (1, 1, 1))
        self.assertEqual({k for k, v in b.cooled.items() if v > now},
                         {"KXA-26NOV09-T1", "KXB-30ARK-Y", "KXC-X", "KXRECENT-1"})
        self.assertEqual({k for k, v in b.event_free.items() if v > now},
                         {"KXA-26NOV09", "KXB-30ARK", "EVC"})
        self.assertIn("t1", b._fills_seen)
        self.assertIn("3 probes, 2 placed, 1 filled", msg)
        d = self.bot()                                  # a dry run counts dry rows only
        d.restore(now)
        self.assertEqual((d.counts["probes"], d.counts["placed"], d.counts["fills"]), (1, 0, 0.0))
        self.assertEqual({k for k, v in d.cooled.items() if v > now}, {"KXD-X"})

    def test_restart_skips_used_markets_and_events(self):
        now = time.time()
        self.write("orders", fp.utc_day(now), [
            {"ts": fp.utc_iso(now - 120), "tid": "r1-1", "event": "placed", "ticker": "KXA-E1-T1"}])
        cs = [cand("KXA-E1-T1", "KXA-E1"), cand("KXA-E1-T2", "KXA-E1"), cand("KXB-E2-T1", "KXB-E2")]
        b = self.bot(live=True, cands=cs, max_open=5)
        b.scanner = FakeScanner(cs, now)
        b.restore(now)
        b.step(now)
        self.assertEqual([t.ticker for t in b.trials.values()], ["KXB-E2-T1"])
        self.assertEqual(b.counts["probes"], 2)


class TestPreflight(unittest.TestCase):
    def test_refusals_and_go(self):
        with tempfile.TemporaryDirectory() as d:
            c, msgs = cfg(), []
            ex = FakeEx(live=True, sub=0)
            self.assertIn("subaccount", fp.live_preflight(ex, c, d, msgs.append))
            ex = FakeEx(live=True, sub=5)
            ex.cash = 1.0
            self.assertIn("fund it", fp.live_preflight(ex, c, d, msgs.append))
            ex.cash = 50.0
            ex.resting = [{"ticker": "X", "client_order_id": "tmm-1", "order_id": "o"}]
            self.assertIn("did not place", fp.live_preflight(ex, c, d, msgs.append))
            ex.resting = [{"ticker": "X", "client_order_id": "fpb-old-1", "order_id": "o9"}]
            ex.positions = [{"ticker": "Y", "position_fp": "3.00"}]
            self.assertIn("never traded", fp.live_preflight(ex, c, d, msgs.append))
            with open(os.path.join(d, "orders_2026-11-08.jsonl"), "w") as f:
                f.write(json.dumps({"event": "placed", "ticker": "Y"}) + "\n")
            self.assertIsNone(fp.live_preflight(ex, c, d, msgs.append))
            self.assertEqual(ex.cancelled, [("o9", "X", None)])


def trow(follow=None, mode="live", **kw):
    r = {"arm": "probe", "mode": mode, "status": "done", "own_key_deltas": 0,
         "unavail_frac": 0.0, "placed": mode == "live",
         "ev": {} if follow is None else {"follow": follow, "join": follow},
         "t1_size": 0.0, "gap_closed": 1.0 if follow is not None else 0.0,
         "stratum": "quiet", "side": "bid", "spread0": 15.0, "series": "KXA", "ticker": "KXA-1",
         "post_s": 300.0}
    r.update(kw)
    return r


class TestReport(unittest.TestCase):
    def test_live_rates_beside_the_baseline(self):
        rows = [trow(5.0 if i < 12 else None) for i in range(20)]
        rows.append(trow(5.0, own_key_deltas=3))                # our own bot: left out
        rows += [trow(None, mode="dry") for _ in range(22)]
        rows += [trow(None, mode="dry", arm="control") for _ in range(2)]   # v1 dry rows
        live = [r for r in rows if r["mode"] == "live" and fp.usable(r)]
        v, lo, hi = fp.boot_rate(live, "join")
        self.assertAlmostEqual(v, 0.6)
        self.assertTrue(0.35 < lo < 0.6 < hi < 0.85)
        self.assertEqual(fp.boot_rate(live, "join"), (v, lo, hi))
        txt = fp.render_report(rows)
        self.assertIn("| joined AT our price | 12 of 20 (60%) |", txt)
        self.assertIn("| 0 of 24 (0%) |", txt)
        self.assertIn("OUR bots", txt)
        self.assertNotIn("control", txt.lower())

    def test_unplaced_live_probe_and_dry_only(self):
        self.assertFalse(fp.usable(trow(5.0, placed=False)))
        txt = fp.render_report([trow(None, mode="dry")])
        self.assertIn("No live probes yet", txt)
        self.assertIn("0 of 1 (0%)", txt)

    def test_cli_report_and_setup(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "trials_2026-11-08.jsonl"), "w") as f:
                for r in (trow(5.0), trow(None, mode="dry")):
                    f.write(json.dumps(r) + "\n")
            self.assertEqual(fp.main(["--report", "--data-dir", d]), 0)
            self.assertTrue(any(n.startswith("report_") for n in os.listdir(d)))
            self.assertEqual(fp.main(["--setup"]), 0)


if __name__ == "__main__":
    unittest.main()
