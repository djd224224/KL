"""The 2026-11-14 market-wave tool update (Jack "Yes, do them"): the pass lock, the
per-wave scan budget in scan_cmd / scan_budget_room, the preflight capacity checks, and
pvd_wave_pace (pace / resize / whydark). No network: kalshi_reads and incentive_mm are
stand-ins; every path points into a temp dir."""
import argparse
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

TMP = tempfile.mkdtemp(prefix="pvdpace_test_")
os.environ["PVD_WAVES_REGISTRY"] = os.path.join(TMP, "waves.json")
os.environ["PVD_PICKOFF_STATE"] = os.path.join(TMP, "pickoff_state.json")

BAL = {"balance_breakdown": [{"exchange_index": 0, "balance": 9000.0}, {"exchange_index": 2, "balance": 400.0}]}
MARKETS = {}


def _fake_reads():
    m = types.ModuleType("kalshi_reads")

    def kalshi_get(path, params=None, prefer="signed", timeout=None):
        if path == "/portfolio/balance":
            return BAL
        if path.startswith("/markets/"):
            return {"market": MARKETS.get(path.split("/")[2], {})}
        return {}
    m.kalshi_get = kalshi_get
    m.kalshi_get_all = lambda path, params=None, items_key="markets", **k: []
    m.read = lambda *a, **k: {}
    return m


sys.modules["kalshi_reads"] = _fake_reads()
_imm = types.ModuleType("incentive_mm")
_imm.company_kpi_series = lambda s: False
_imm.series_blocklisted = lambda s: False
sys.modules["incentive_mm"] = _imm

import pvd_waves as pw  # noqa: E402
import pvd_wave_pace as pp  # noqa: E402

INC = os.path.join(TMP, "inc")
os.makedirs(INC, exist_ok=True)
pw.INC = INC
pw.STATE_FILE = os.path.join(INC, "imm_state.json")
pw.LIST = os.path.join(INC, "post_vote_dead_books.json")
pw.LAUNCHER = os.path.join(TMP, "run_incentive_mm.ps1")
pw.SCAN_LOCK = os.path.join(TMP, "scan.lock")
NOW = datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc)
LATER = (NOW + timedelta(hours=15)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wave(**kw):
    w = {"series": ["KXFOO"], "near": [], "since": "2026-11-13T17:30:00Z", "port": 8799, "dir": "pvd-watch-wt",
         "scan_size": 200, "budget": 50000.0, "stop_cap": None, "preset": "default",
         "tune": list(pw.PRESETS["default"]), "near_tune": list(pw.PRESETS["near"]), "enabled": True,
         "added": "2026-11-13T17:40:00Z"}
    w.update(kw)
    return w


def _save_reg(waves):
    with open(os.environ["PVD_WAVES_REGISTRY"], "w", encoding="utf-8") as fh:
        json.dump({"waves": waves}, fh)


class ScanBudget(unittest.TestCase):
    def test_scan_cmd_passes_the_waves_own_budget_and_series(self):
        w = _wave(near=["KXBAR"], budget=50000.0)
        _cmd, env = pw.scan_cmd(w, write=False)
        self.assertEqual(env["PVD_SCAN_OWN_BUDGET"], "50000")
        self.assertEqual(env["PVD_SCAN_OWN_SERIES"], "KXFOO,KXBAR")
        self.assertEqual(env["PVD_SCAN_BUDGET"], str(int(pw.GLOBAL_BUDGET + 50000)))

    def test_room_is_the_larger_of_the_shared_and_own_rules(self):
        L = {"KXFOO-1": {"added": "scan 2026-11-14T10:00Z", "stop": LATER, "coll_usd": 10_000},
             "KXOTHER-1": {"added": "scan 2026-11-14T10:00Z", "stop": LATER, "coll_usd": 681_453}}
        r = pw.scan_budget_room(_wave(budget=50000.0), L=L, now=NOW)
        self.assertEqual(r["live_all"], 691_453)
        self.assertEqual(r["live_own"], 10_000)
        self.assertLess(r["shared_room"], 0)
        self.assertEqual(r["room"], 40_000)


class PassLock(unittest.TestCase):
    def setUp(self):
        _save_reg({"wt": _wave()})
        for f in os.listdir(TMP):
            if f.startswith("wave_pass_"):
                os.remove(os.path.join(TMP, f))

    def test_lock_lives_beside_the_registry(self):
        self.assertEqual(os.path.dirname(pw.pass_lock_path("wt")), TMP)

    def test_a_held_lock_blocks_then_a_stale_one_is_taken(self):
        p = pw.pass_lock_path("wt")
        with open(p, "w") as fh:
            fh.write("1 x run\n")
        with self.assertRaises(pw.PassLockBusy):
            with redirect_stdout(io.StringIO()):
                with pw.wave_pass_lock("wt", 0.2, "test"):
                    pass
        old = time.time() - pw.PASS_LOCK_STALE_S - 5
        os.utime(p, (old, old))
        with pw.wave_pass_lock("wt", 0.2, "test"):
            self.assertTrue(os.path.exists(p))
        self.assertFalse(os.path.exists(p))

    def test_setarg_waits_for_a_running_pass_and_gives_up_without_writing(self):
        p = pw.pass_lock_path("wt")
        with open(p, "w") as fh:
            fh.write("1 x run\n")
        old_wait = pw.REG_EDIT_WAIT_S
        pw.REG_EDIT_WAIT_S = 0.2
        try:
            a = argparse.Namespace(name="wt", flag="cheap-tiers", value="2:20,5:10", unset=False, group="main")
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                pw.cmd_setarg(a)
            self.assertIn("2:10,5:5", json.dumps(pw.load()["waves"]["wt"]["tune"]))
            os.remove(p)
            with redirect_stdout(io.StringIO()):
                pw.cmd_setarg(a)
            self.assertIn("2:20,5:10", json.dumps(pw.load()["waves"]["wt"]["tune"]))
            self.assertFalse(os.path.exists(p))
        finally:
            pw.REG_EDIT_WAIT_S = old_wait

    def test_the_task_pass_skips_a_wave_another_pass_holds(self):
        p = pw.pass_lock_path("wt")
        with open(p, "w") as fh:
            fh.write("1 x run\n")
        called = []
        real = pw._run_wave
        pw._run_wave = lambda *a, **k: called.append(1) or 0
        real_one = pw.run_one
        pw.run_one = lambda *a, **k: 0
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                pw.cmd_run(argparse.Namespace(write=True, only=None, wait_lock=None))
            self.assertEqual(called, [])
            self.assertIn("another pass is running it", out.getvalue())
        finally:
            pw._run_wave, pw.run_one = real, real_one
            os.remove(p)

    def test_the_pass_reads_the_args_once_it_holds_the_lock(self):
        seen = []
        real = pw._run_wave
        real_one = pw.run_one
        pw._run_wave = lambda name, w, a, lk=None: seen.append(json.dumps(w["tune"])) or 0
        pw.run_one = lambda *a, **k: 0
        try:
            reg = pw.load()
            reg["waves"]["wt"]["tune"] = [x if x != "2:10,5:5" else "2:30,5:15" for x in reg["waves"]["wt"]["tune"]]
            pw.save(reg)
            with redirect_stdout(io.StringIO()):
                pw.cmd_run(argparse.Namespace(write=True, only="wt", wait_lock=0))
            self.assertIn("2:30,5:15", seen[0])
        finally:
            pw._run_wave, pw.run_one = real, real_one


class Capacity(unittest.TestCase):
    def setUp(self):
        self._bl, self._le = pw.bot_lines, pw.launcher_env
        stamp = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%SZ")
        self.lines = {
            pw.CYCLE_MARK: f"{stamp} [IMM] 4810/6459 mkts quoted, 11741 resting, 118 amend",
            pw.UNIVERSE_MARK: (f"{stamp} [IMM] universe: 10960 program markets -> 7711 candidates -> 6149 selected "
                               "across 980/1000 events (0 forced quote-all @ ~$312416, total ~$2,950,000 ladder "
                               "collateral, $97961 inventory reserve); skips {'payout_floor': 526, 'budget': 12}"),
            pw.PLACE_CAP_MARK: f"{stamp} [IMM] placement cap 1500/cycle reached; 812 deferred to next cycle "
                               "(dark side 300, renewal 512)",
        }
        pw.bot_lines = lambda marks: {m: self.lines[m] for m in marks if m in self.lines}
        pw.launcher_env = lambda: {"IMM_MAX_TOTAL_RESTING": "12000", "IMM_COLLATERAL_BUDGET": "3000000",
                                   "IMM_MAX_PLACEMENTS_PER_CYCLE": "1500"}
        with open(pw.LIST, "w", encoding="utf-8") as fh:
            json.dump({"markets": {"KXGOLDEVENT-17NOV261100-T1": {"added": "scan 2026-11-14T10:00Z", "stop": LATER,
                                                                  "coll_usd": 100}}}, fh)
        MARKETS["KXGOLDEVENT-17NOV261100-T1"] = {"exchange_index": 2}

    def tearDown(self):
        pw.bot_lines, pw.launcher_env = self._bl, self._le

    def test_capacity_flags_room_budget_events_queue_and_the_waves_shard(self):
        out = pw.capacity_checks([("wg", _wave(series=["KXGOLDEVENT"]))], auto=True)
        got = {n: (s, d) for s, n, d in out}
        self.assertEqual(got["resting-order room"][0], pw.FAIL)           # 259 left < 500, auto
        self.assertEqual(got["collateral budget"][0], pw.WARN)            # 12 refused
        self.assertIn("12 market(s) refused", got["collateral budget"][1])
        self.assertEqual(got["event ceiling"][0], pw.WARN)                # 980 of 1000
        self.assertIn("812 placements deferred", got["placement queue"][1])
        self.assertEqual(got["wg shard-2 cash"][0], pw.FAIL)              # $400 < $1,000, auto
        self.assertEqual(got["wg scan budget"][0], pw.PASS)

    def test_manual_mode_warns_instead(self):
        out = pw.capacity_checks([("wg", _wave(series=["KXGOLDEVENT"]))], auto=False)
        got = {n: s for s, n, _d in out}
        self.assertEqual(got["resting-order room"], pw.WARN)
        self.assertEqual(got["wg shard-2 cash"], pw.WARN)

    def test_a_truncated_order_read_since_the_start_fails(self):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        self.lines[pw.TRUNC_MARK] = f"{stamp} [IMM] ! RESTING_READ_TRUNCATED: 40 pages of 1000"
        self.lines["=== incentive_mm"] = "2026-11-14 01:00:00Z === incentive_mm start"
        out = pw.capacity_checks([], auto=False)
        self.assertIn((pw.FAIL, "resting-order read"), [(s, n) for s, n, _d in out])


def _rows(n, own=(200, 200), want=(200, 200), rate=0.5, pool=13.7, rest_every=1, t0=None):
    t0 = t0 or NOW - timedelta(minutes=60)
    out = []
    for i in range(n):
        resting = (i % rest_every) == 0
        out.append({"ts": (t0 + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M:%S"),
                    "b": own[0] if resting else 0, "a": own[1] if resting else 0, "wb": want[0], "wa": want[1],
                    "rate": rate if resting else 0.0, "pool": pool})
    return out


class Judge(unittest.TestCase):
    def test_landing_when_the_latest_size_hasnt_rested(self):
        rows = _rows(6) + [{"ts": "x", "b": 200, "a": 200, "wb": 600, "wa": 600, "rate": 0.5, "pool": 13.7}]
        self.assertEqual(pp.judge_rows(rows)["status"], "landing")

    def test_average_is_weighted_by_how_often_it_rests(self):
        j = pp.judge_rows(_rows(10, rate=1.0, rest_every=2))
        self.assertEqual(j["status"], "judged")
        self.assertAlmostEqual(j["uptime"], 0.5)
        self.assertAlmostEqual(j["rate"], 0.5)
        self.assertAlmostEqual(j["share"], 1.0 / 13.7, places=4)

    def test_only_rows_after_the_size_landed_count(self):
        early = _rows(4, own=(100, 100), want=(300, 300), rate=0.1)
        late = _rows(4, own=(300, 300), want=(300, 300), rate=1.0, t0=NOW - timedelta(minutes=20))
        j = pp.judge_rows(early + late)
        self.assertEqual(j["rows"], 4)
        self.assertAlmostEqual(j["rate"], 1.0)


class Multiple(unittest.TestCase):
    J = {"share": 0.06, "pool": 13.7, "uptime": 1.0}

    def test_r1_is_the_current_projection(self):
        self.assertAlmostEqual(pp.proj_at(1.0, self.J, 0.2, 15.0, 0.65), 0.2 + 13.7 * 0.06 * 15 / 24)

    def test_the_model_saturates_and_counts_only_part_of_the_gain(self):
        # 5x on a 3% share, 65% counted: 0.03 -> 0.134 modelled, 0.098 counted (not 0.15)
        p5 = pp.proj_at(5.0, {"share": 0.03, "pool": 13.7, "uptime": 1.0}, 0.0, 24.0, 0.65)
        self.assertAlmostEqual(p5, 13.7 * (0.03 + 0.65 * (0.15 / 1.12 - 0.03)), places=6)

    def test_needed_multiple_is_the_smallest_quarter_step(self):
        x, p_max = pp.needed_mult(self.J, 0.2, 15.0, 1.0, 1.20, 5.0, 0.65)
        self.assertIsNotNone(x)
        self.assertEqual(round(x * 4), x * 4)
        self.assertGreaterEqual(pp.proj_at(x, self.J, 0.2, 15.0, 0.65), 1.20)
        self.assertLess(pp.proj_at(x - 0.25, self.J, 0.2, 15.0, 0.65), 1.20)
        self.assertGreaterEqual(p_max, 1.20)

    def test_none_when_the_cap_cannot_reach_the_aim(self):
        x, p_max = pp.needed_mult({"share": 0.002, "pool": 13.7, "uptime": 1.0}, 0.0, 15.0, 1.0, 1.20, 5.0, 0.65)
        self.assertIsNone(x)
        self.assertLess(p_max, 1.0)


def _m(t, proj, x_aim=None, proj_max=0.0, m0=1.0, status="judged"):
    return {"ticker": t, "series": t.split("-")[0], "status": status, "proj": proj, "x_aim": x_aim,
            "proj_max": proj_max, "m0": m0}


class Plan(unittest.TestCase):
    A = argparse.Namespace(aim=1.20, max_mult=5.0, keep_within=0.05, no_off=False)

    def test_up_off_two_reads_borderline_and_keep(self):
        res = {"markets": [_m("KXA-1", 1.5), _m("KXA-2", 0.6, x_aim=2.5, proj_max=1.9),
                           _m("KXA-3", 0.5, x_aim=None, proj_max=1.05), _m("KXA-4", 0.2, proj_max=0.4),
                           _m("KXA-5", 0.97, proj_max=0.97, m0=5.0), _m("KXA-6", 0.1, proj_max=0.3),
                           _m("KXA-7", 0.0, status="landing")]}
        prev = {"at": (NOW - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ"), "off_candidates": ["KXA-6"]}
        p = pp.plan_resize(res, prev, self.A, NOW)
        self.assertEqual(p["up"], {"KXA-2": 2.5, "KXA-3": 5.0})
        self.assertEqual(p["off"], ["KXA-6"])                 # under $1 at 5x on two reads
        self.assertEqual(p["off_next"], ["KXA-4"])            # first read: waits for the next
        self.assertEqual(p["borderline"], 1)                  # $0.97 at the cap: kept (noise)
        self.assertEqual(p["keep"], 1)
        self.assertEqual(p["skipped"]["landing"], 1)

    def test_a_previous_read_too_old_or_too_new_doesnt_count(self):
        res = {"markets": [_m("KXA-6", 0.1, proj_max=0.3)]}
        for mins in (2, 120):
            prev = {"at": (NOW - timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "off_candidates": ["KXA-6"]}
            p = pp.plan_resize(res, prev, self.A, NOW)
            self.assertEqual(p["off"], [])
            self.assertEqual(p["off_next"], ["KXA-6"])


class WhyDark(unittest.TestCase):
    def _ctx(self, **kw):
        c = {"pos": {}, "holds": {}, "sel": {}, "guards": {}, "log": {}, "books": {}, "cycle": {}, "wave_off": None}
        c.update(kw)
        return c

    def test_reasons_in_order(self):
        e = {"bid_max": 30, "ask_min": 40, "stop": LATER}
        self.assertEqual(pp.side_reason("T-1", "bid", dict(e, bid_size=0), self._ctx())[1], "off")
        self.assertEqual(pp.side_reason("T-1", "bid", e, self._ctx(pos={"T-1": 200}))[1], "held")
        self.assertIn("only the closing side", pp.side_reason("T-1", "bid", e, self._ctx(pos={"T-1": 200}))[0])
        self.assertEqual(pp.side_reason("T-1", "bid", e, self._ctx(sel={"T-1": "payout_floor"}))[1], "selection")
        self.assertEqual(pp.side_reason("T-1", "ask", e, self._ctx(log={("T-1", "ask"): "lone-ref guard: x"}))[1],
                         "log")
        bk = {"T-1": {"status": "active", "yes_bid_dollars": "0.35", "yes_ask_dollars": "0.38"}}
        r = pp.side_reason("T-1", "bid", dict(e, fair_clamp="x"), self._ctx(books=bk))
        self.assertEqual(r[1], "limit")
        self.assertIn("fair clamp", r[0])
        cy = {"T-1": {"b": 0, "a": 200, "wb": 300, "wa": 200}}
        bk2 = {"T-1": {"status": "active", "yes_bid_dollars": "0.25", "yes_ask_dollars": "0.45"}}
        self.assertEqual(pp.side_reason("T-1", "bid", e, self._ctx(books=bk2, cycle=cy))[1], "queue")
        cy0 = {"T-1": {"b": 0, "a": 200, "wb": 0, "wa": 200}}
        self.assertEqual(pp.side_reason("T-1", "bid", e, self._ctx(books=bk2, cycle=cy0))[1], "sized0")
        self.assertEqual(pp.side_reason("T-1", "bid", e, self._ctx(books={"T-1": {"status": "finalized"}}))[1],
                         "closed")

    def test_log_lines_set_and_clear_per_side(self):
        p = os.path.join(INC, "incentive-mm-2026-11-14.log")
        now = datetime.now(timezone.utc)
        s = lambda m: (now - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%SZ")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(f"{s(9)} [IMM] pvd guard T-1: ask out (limits bid<=55 ask>=98, last None, book 52x97)\n")
            fh.write(f"{s(8)} [IMM] lone-ref guard T-2 bid: the reference 35c rests on 296 ... -- stands aside\n")
            fh.write(f"{s(7)} [IMM] lone-ref guard (dry) T-3 ask: the reference ... -- would stand aside\n")
            fh.write(f"{s(6)} [IMM] crypto-event stand-aside T-4: SOL jump hold (8m left)\n")
            fh.write(f"{s(5)} [IMM] pvd guard T-5: bid/ask out (limits bid<=1 ask>=99, last 50, book 40x60)\n")
            fh.write(f"{s(4)} [IMM] pvd guard T-5: clear\n")
        got = pp.log_side_reasons(["T-1", "T-2", "T-3", "T-4", "T-5"], now)
        self.assertIn(("T-1", "ask"), got)
        self.assertIn(("T-2", "bid"), got)
        self.assertNotIn(("T-3", "ask"), got)             # dry: logged only
        self.assertIn(("T-4", "bid"), got)
        self.assertIn(("T-4", "ask"), got)
        self.assertNotIn(("T-5", "bid"), got)             # cleared
        os.remove(p)


if __name__ == "__main__":
    unittest.main()
