"""Tests for ws_stale_score.py on a synthetic day whose answer is worked out
by hand (the reward shares from _side_share's own rules)."""

import csv
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import incentive_mm as imm
import ws_stale_score as wss

T = datetime(2026, 10, 4, 12, 0, 0, tzinfo=timezone.utc).timestamp()
A, B = "KXTEST-26OCT04-A", "KXTEST-26OCT04-B"
EV = "KXTEST-26OCT04"


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag(ts, oid, side, px, rem, yes, no, ours, ticker=A, run="r1", mode="dry",
          gap=2.0, phase=None):
    r = {"ev": "flag", "ts": ts, "mode": mode, "ticker": ticker, "order_id": oid,
         "side": side, "px": px, "rem": rem, "age_s": 100.0, "ahead_s": 1.5,
         "ext_bid": None, "ext_ask": None, "gap_c": gap, "yes": yes, "no": no,
         "ours": ours, "run_id": run}
    if phase:
        r["phase"] = phase           # flags before 10/4 evening carry none
    return r


def _end(ts, ev, oid, side, px, stale, ticker=A, run="r1", **kw):
    r = {"ev": ev, "ts": ts, "ticker": ticker, "order_id": oid, "side": side,
         "px": px, "stale_s": stale, "run_id": run}
    r.update(kw)
    return r


def _fill(ts, fid, oid, ticker, side, px, n):
    return {"ts": ts, "fill_id": fid, "order_id": oid, "ticker": ticker,
            "count": n, "yes_price_cents": px, "is_taker": False,
            "our_book_side": side}


BOOK_A = dict(yes=[[49.0, 20.0], [47.0, 500.0]], no=[[49.0, 1200.0]],
              ours=[["bid", 49.0, 20.0]])
# A (target 100, df 0.5, pool $86.4/day = $0.001/s): our 20 alone at 49 is
# 20/145 of the YES side, NO side 0 -> frac 0.0689655; without it 0
RATE_A = 0.001 * (20 / 145) / 2
# o2's ask at 53 is a NO bid at 47, the NO ref is 45 (cum 410 >= 20): 10/410
RATE_O2 = 0.001 * (10 / 410) / 2
# B (pool $172.8/day = $0.002/s): YES ref 28, our 15 at 30 full weight: 15/1015
RATE_O3 = 0.002 * (15 / 1015) / 2


def _write_day(d):
    stale = [
        _flag(T, "o1", "bid", 49.0, 20.0, **BOOK_A),
        # the bot stamps a cycle's amend with the cycle's START (T+8.8); the
        # amend itself went out at T+60 (orders_*.jsonl)
        _end(T + 8.8, "amend", "o1", "bid", 49.0, 8.8, new_px=47.0),
        _flag(T + 600, "o2", "ask", 53.0, 10.0, yes=[[50.0, 300.0]],
              no=[[47.0, 10.0], [45.0, 400.0]], ours=[["ask", 53.0, 10.0]]),
        _end(T + 605, "clear", "o2", "ask", 53.0, 5.0),
        # re-flagged inside o2's window (T+600 .. T+660): folded
        _flag(T + 610, "o2", "ask", 53.0, 10.0, yes=[[50.0, 300.0]],
              no=[[47.0, 10.0], [45.0, 400.0]], ours=[["ask", 53.0, 10.0]]),
        _end(T + 615, "clear", "o2", "ask", 53.0, 5.0),
        _flag(T + 1200, "o3", "bid", 30.0, 15.0, ticker=B,
              yes=[[30.0, 15.0], [28.0, 1000.0]], no=[[60.0, 1000.0]],
              ours=[["bid", 30.0, 15.0]], phase="cycle"),       # never ends: open
        _flag(T + 2000, "o5", "bid", 49.0, 20.0, **BOOK_A),
        _end(T + 2030, "gone", "o5", "bid", 49.0, 30.0),
        # an ask already lifted mid-cycle: no NO size at 84c in its book
        _flag(T + 2400, "o7", "ask", 16.0, 25.0, yes=[[14.0, 1000.0]],
              no=[[70.0, 1000.0]], ours=[["ask", 16.0, 25.0]]),
        _end(T + 2460, "gone", "o7", "ask", 16.0, 60.0),
        _flag(T + 2500, "o6", "bid", 49.0, 20.0, mode="live", **BOOK_A),
        _end(T + 2500, "cancel", "o6", "bid", 49.0, 0.0, by="fast"),
    ]
    with open(os.path.join(d, "ws_stale_2026-10-04.jsonl"), "w") as f:
        for r in stale:
            f.write(json.dumps(r) + "\n")
    fills = [
        _fill(T - 2, "f0", "o1", A, "bid", 49.0, 5),     # before the flag: its cause
        _fill(T + 30, "f1", "o1", A, "bid", 49.0, 10),   # avoidable
        _fill(T + 90, "f2", "o1", A, "bid", 47.0, 10),   # after the amend, new price
        _fill(T + 1230, "f3", "o3", B, "bid", 30.0, 15),  # avoidable (open episode)
        _fill(T + 2010, "f5", "o5", A, "bid", 49.0, 20),  # avoidable, then gone
        _fill(T + 2010, "f5", "o5", A, "bid", 49.0, 20),  # the sink wrote it twice
    ]
    # the event sweep breaker's dry trips (A and B are one event)
    trips = [
        {"ev": "trip", "ts": T + 20, "mode": "dry", "event": EV, "ticker": A,
         "order_id": "o0", "side": "bid", "px": 49.0, "count": 20.0, "hold_s": 300.0,
         "pull": [[B, "ob", "bid", 30.0, 15.0]], "run_id": "r1"},
        {"ev": "trip", "ts": T + 2000, "mode": "dry", "event": EV, "ticker": A,
         "order_id": "o8", "side": "ask", "px": 51.0, "count": 5.0, "hold_s": 300.0,
         "pull": [], "run_id": "r1"}]
    with open(os.path.join(d, "ws_sweep_2026-10-04.jsonl"), "w") as f:
        for r in trips:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(d, "fills_2026-10-04.jsonl"), "w") as f:
        for r in fills:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(d, "orders_2026-10-04.jsonl"), "w") as f:
        for ts, kind, oid in ((T - 100, "place", "o1"), (T + 60, "amend", "o1"),
                              (T + 3000, "cancel", "o1"), (T + 30, "amend", "zz")):
            f.write(json.dumps({"ts": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                                "kind": kind, "order_id": oid, "ticker": A}) + "\n")
    hdr = imm.IncentiveMarketMaker.CYCLE_LOG_HEADER.strip().split(",")
    with open(os.path.join(d, "cycle_log_2026-10-04.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(hdr)
        for k in range(-10, 121):
            ts = T + 60 * k
            for tk, pool, (b, a) in (
                    (A, 86.4, (47, 51) if ts < T + 330 else (44, 46)),
                    (B, 172.8, (29, 31) if ts < T + 1500 else (20, 22))):
                row = {c: "" for c in hdr}
                row.update(ts=_iso(ts), ticker=tk, ext_bid=b, ext_ask=a, target=100,
                           discount=0.5, pool_per_day=pool, est_frac=0.05)
                w.writerow([row[c] for c in hdr])
            if k == 0:
                w.writerow(hdr)          # a schema widening re-emits it inline


class TestScore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = self.tmp.name
        _write_day(self.d)

    def _summary(self, actions=True):
        eps = wss.load_episodes(self.d, None, None)
        oids = {e["flag"]["order_id"] for e in eps}
        fills = wss.load_fills(self.d, oids)
        cyc = wss.load_cycle_rows(self.d, {A, B}, T - 3600, T + 9000)
        acts = wss.load_order_actions(self.d, oids, T - 60, T + 9000) if actions else {}
        return wss.summarize(wss.score(eps, fills, cyc, acts), T, T + 3600)

    def test_cycle_actions_are_timed_from_the_orders_sink(self):
        self.assertEqual(self._summary(actions=False)["D"], 8.8)   # cycle start
        self.assertEqual(self._summary()["D"], 60.0)              # the amend itself

    def test_the_hand_worked_day(self):
        s = self._summary()
        t = s["total"]
        self.assertEqual(s["D"], 60.0)                    # o1's amend
        self.assertEqual((s["folded"], s["live"], s["phantom"]), (1, 1, 1))
        self.assertEqual(s["by_end"], {"amend": 1, "clear": 1, "open": 1, "gone": 1})
        self.assertEqual((t["episodes"], t["hit"], t["avoid_ct"], t["while_stale_ct"]),
                         (4, 3, 45.0, 45.0))
        self.assertEqual(s["fills_before_ct"], 5.0)
        # 10 @49 marked 45, 15 @30 marked 21, 20 @49 marked 45 -- all losers
        for h in wss.HORIZONS:
            self.assertAlmostEqual(t[f"avoided_{h}"], 0.40 + 1.35 + 0.80, places=9)
        cost = RATE_A * 60 + RATE_O2 * 60 + RATE_O3 * 60 + RATE_A * 30
        self.assertAlmostEqual(t["reward_cost"], cost, places=12)
        self.assertAlmostEqual(t["net_300"], 2.55 - cost, places=9)
        self.assertEqual(t["reward_unscored"], 0)
        self.assertAlmostEqual(s["per_day"]["avoided_300"], 2.55 * 24, places=6)

    def test_replace_windows_follow_how_each_episode_ended(self):
        eps = wss.load_episodes(self.d, None, None)
        oids = {e["flag"]["order_id"] for e in eps}
        fills = wss.load_fills(self.d, oids)
        cyc = wss.load_cycle_rows(self.d, {A, B}, T - 3600, T + 9000)
        acts = wss.load_order_actions(self.d, oids, T - 60, T + 9000)
        got = {r["order_id"]: (r["end"], r.get("replace_s"))
               for r in wss.score(eps, fills, cyc, acts)["episodes"]
               if not (r.get("folded") or r.get("live") or r.get("phantom"))}
        self.assertEqual(got, {"o1": ("amend", 60.0), "o2": ("clear", 60.0),
                               "o3": ("open", 60.0), "o5": ("gone", 30.0)})

    def test_reward_rate_is_the_models_share_with_minus_without(self):
        self.assertAlmostEqual(
            wss.reward_rate(_flag(T, "o1", "bid", 49.0, 20.0, **BOOK_A), 100, 0.5, 86.4),
            RATE_A, places=15)
        # a side the rung alone holds above target: without it the snapshot
        # is excluded, so the whole market's share goes
        thin = _flag(T, "x", "bid", 49.0, 100.0, yes=[[49.0, 100.0]],
                     no=[[49.0, 1200.0]], ours=[["bid", 49.0, 100.0]])
        self.assertAlmostEqual(wss.reward_rate(thin, 100, 0.5, 86.4), 0.001 * 0.5,
                               places=15)
        self.assertIsNone(wss.reward_rate(thin, None, 0.5, 86.4))

    def test_own_cents_floor_like_the_book(self):
        self.assertEqual([wss._own_cents("bid", 4.29), wss._own_cents("ask", 4.29),
                          wss._own_cents("bid", 49.0), wss._own_cents("ask", 53.0)],
                         [4, 5, 49, 53])

    def test_a_since_window_and_main(self):
        eps = wss.load_episodes(self.d, T + 1000, None)
        self.assertEqual(sorted(e["flag"]["order_id"] for e in eps),
                         ["o3", "o5", "o6", "o7"])
        out = os.path.join(self.d, "s.json")
        self.assertEqual(wss.main(["--dir", self.d, "--json", out]), 0)
        with open(out) as f:
            self.assertEqual(json.load(f)["total"]["episodes"], 4)
        empty = tempfile.mkdtemp()
        self.assertEqual(wss.main(["--dir", empty]), 1)
        os.rmdir(empty)

    def test_the_phase_table_splits_in_cycle_from_idle(self):
        s = self._summary()
        self.assertEqual({k: v["episodes"] for k, v in s["by_phase"].items()},
                         {"cycle": 1, "idle": 3})
        self.assertAlmostEqual(s["by_phase"]["cycle"]["avoided_300"], 1.35, places=9)

    def test_sweep_trips_are_scored_like_the_backtest(self):
        trips = wss.load_sweeps(self.d, None, None)
        efills = wss.load_event_fills(self.d, {EV})
        self.assertEqual(len(efills[EV]), 5)               # f5 once
        cyc = wss.load_cycle_rows(self.d, {A, B}, T - 3600, T + 9000)
        res = wss.score_sweeps(trips, efills, cyc)
        # trip 1 (T+20, 300s): f1 @T+30 10 @49 and f2 @T+90 10 @47 (any of our
        # fills in the event), both marked 45: -$0.40 - $0.20; A + B at
        # 4.32 + 8.64 $/day of reward for 300s
        self.assertEqual([r["avoid_n"] for r in res], [2, 1])
        self.assertAlmostEqual(res[0]["pnl"][300], -0.60, places=9)
        self.assertAlmostEqual(res[0]["cost"], (4.32 + 8.64) / 86400 * 300, places=9)
        # trip 2 (T+2000): f5 @T+2010 (once, though written twice) -> -$0.80
        self.assertAlmostEqual(res[1]["pnl"][1800], -0.80, places=9)
        self.assertAlmostEqual(res[1]["cost"], 4.32 / 86400 * 300, places=9)
        text = wss.render_sweeps(res, 1.0)
        self.assertIn("Event sweep breaker", text)
        self.assertIn("$+1.40", text)                         # avoided 5m

    def test_main_reports_the_sweeps_too(self):
        out = os.path.join(self.d, "s.json")
        self.assertEqual(wss.main(["--dir", self.d, "--json", out]), 0)
        with open(out) as f:
            self.assertEqual(len(json.load(f)["sweeps"]), 2)


if __name__ == "__main__":
    unittest.main()
