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

    def test_scan_max_mult_passes_the_bank_max_and_the_multiples_file(self):
        # Jack 2026-11-18 "1.5-3x the size": the FX wave lists an under-bar strike at up to
        # 2x its 300 bank max (1,500-3,000 a side at 3-5c with its 5x cheap tier)
        mf = os.path.join(TMP, "size_mult_wt.json")
        tune = ["--bank-size", "200", "--bank-max", "300", "--cheap-tiers", "2:10,5:5", "--size-mult-file", mf]
        _c, env = pw.scan_cmd(_wave(tune=tune, scan_max_mult=2.0), write=False)
        self.assertEqual((env["PVD_SCAN_MAX_MULT"], env["PVD_SCAN_MULT_BASE"], env["PVD_SCAN_MULT_FILE"]),
                         ("2", "300", mf))
        _c, env = pw.scan_cmd(_wave(tune=tune + ["--size-mult", "1.5"], scan_max_mult=2.0), write=False)
        self.assertEqual(env["PVD_SCAN_MULT_BASE"], "450")
        for w in (_wave(tune=tune), _wave(tune=tune[:6], scan_max_mult=2.0)):    # off; no multiples file
            _c, env = pw.scan_cmd(w, write=False)
            self.assertNotIn("PVD_SCAN_MAX_MULT", env)
        _c, env = pw.scan_cmd(_wave(tune=tune, near=["KXBAR"], scan_max_mult=2.0), write=False, group="near")
        self.assertNotIn("PVD_SCAN_MAX_MULT", env)

    def test_set_scan_max_mult_needs_a_multiples_file_and_stays_within_5x(self):
        _save_reg({"wt": _wave(), "wm": _wave(tune=list(pw.PRESETS["default"]) + ["--size-mult-file", "m.json"])})
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                pw._set(argparse.Namespace(name="wt", key="scan_max_mult", value="2"))
            with self.assertRaises(SystemExit):
                pw._set(argparse.Namespace(name="wm", key="scan_max_mult", value="6"))
            pw._set(argparse.Namespace(name="wm", key="scan_max_mult", value="2"))
        self.assertEqual(pw.load()["waves"]["wm"]["scan_max_mult"], 2.0)

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

    def test_held_while_the_bot_rests_its_base_under_the_listed_size(self):
        # 11/18 w1113-fxmetals: the bot's first hour rested 300 a side against 1,500 listed;
        # a share read at 300 is no read of the listed size, so the market is not judged
        j = pp.judge_rows(_rows(6, own=(300, 300), want=(300, 300), rate=0.07), (1500.0, 1500.0))
        self.assertEqual(j["status"], "held")
        self.assertEqual((j["own"], j["want"], j["list"]), (600, 600, 3000.0))

    def test_only_rows_after_the_bot_wants_the_listed_size_count(self):
        early = _rows(4, own=(300, 300), want=(300, 300), rate=0.07)
        late = _rows(3, own=(1500, 1500), want=(1500, 1500), rate=0.41, t0=NOW - timedelta(minutes=15))
        j = pp.judge_rows(early + late, (1500.0, 1500.0))
        self.assertEqual((j["status"], j["rows"]), ("judged", 3))
        self.assertAlmostEqual(j["rate"], 0.41)

    def test_a_side_the_bot_does_not_quote_is_not_held(self):
        # a fair clamp or a capped side: the bot wants 0 there, which is not a base hold
        j = pp.judge_rows(_rows(4, own=(0, 300), want=(0, 300)), (1500.0, 300.0))
        self.assertEqual(j["status"], "judged")

    def test_the_hour_multiple_scales_the_listed_size(self):
        rows = [dict(r, hm=0.5) for r in _rows(4, own=(150, 150), want=(150, 150))]
        self.assertEqual(pp.judge_rows(rows, (300.0, 300.0))["status"], "judged")
        self.assertEqual(pp.judge_rows(_rows(4, own=(150, 150), want=(150, 150)), (300.0, 300.0))["status"],
                         "held")

    def test_without_the_listed_sizes_nothing_is_held(self):
        self.assertEqual(pp.judge_rows(_rows(4, own=(300, 300), want=(300, 300)))["status"], "judged")


class MeasureHeld(unittest.TestCase):
    def test_a_market_the_bot_holds_at_base_is_neither_judged_nor_turned_off(self):
        # the 11/18 FX offs: 300 a side resting, 1,500 listed (cheap tier x5), two reads
        _save_reg({"wt": _wave()})
        L = {"KXFOO-26NOV-T1": {"stop": LATER, "size": 300, "bid_size": 1500, "ask_size": 1500},
             "KXFOO-26NOV-T2": {"stop": LATER, "size": 300}}
        rows = {t: _rows(6, own=(300, 300), want=(300, 300), rate=0.07, pool=17.57) for t in L}
        a = argparse.Namespace(aim=1.20, max_mult=5.0, real=0.65, window_min=60, keep_within=0.05, no_off=False)
        res = pp.measure("wt", a, now=NOW, dash={"now": NOW.isoformat(), "rows": []}, rows=rows,
                         bot={"started": NOW.timestamp() - 7200}, mult={}, L=L)
        self.assertEqual({m["ticker"]: m["status"] for m in res["markets"]},
                         {"KXFOO-26NOV-T1": "held", "KXFOO-26NOV-T2": "judged"})
        prev = {"at": (NOW - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "off_candidates": ["KXFOO-26NOV-T1", "KXFOO-26NOV-T2"]}
        plan = pp.plan_resize(res, prev, a, NOW)
        self.assertEqual(plan["off"], ["KXFOO-26NOV-T2"])         # the one judged at its listed size
        self.assertEqual(plan["skipped"]["held"], 1)
        out = io.StringIO()
        with redirect_stdout(out):
            pp.print_pace(dict(res, dash_at=None, bot_started=None, since_restart_min=120.0, rows_since=None,
                               dash_age_min=1.0, listed=2))
        self.assertIn("1 held under the listed size by the bot", out.getvalue())

    def test_only_rows_since_the_last_size_change_and_a_pending_restore_is_not_judged(self):
        # 11/18 20:16:39Z: 99 FX strikes back from a 0 were judged on the half hour they rested
        # nothing while pinned off, 3 s before the tune brought them back
        _save_reg({"wt": _wave()})
        off = _rows(6, own=(0, 0), want=(0, 0), rate=0.0, pool=17.57)                   # pinned at 0
        back = _rows(3, own=(1500, 1500), want=(1500, 1500), rate=0.41, pool=17.57,
                     t0=NOW - timedelta(minutes=15))
        L = {"KXFOO-26NOV-T1": {"stop": LATER, "size": 300, "bid_size": 1500, "ask_size": 1500,
                                "unpinned": (NOW - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
                                + ": back on from the multiples file's 0"},
             "KXFOO-26NOV-T2": {"stop": LATER, "size": 0, "pinned": "0 a side",
                                "pre_off": {"size": 300, "bid_size": 1500, "ask_size": 1500}}}
        rows = {"KXFOO-26NOV-T1": off + back, "KXFOO-26NOV-T2": off}
        a = argparse.Namespace(aim=1.20, max_mult=5.0, real=0.65, window_min=60, keep_within=0.05, no_off=False)
        res = pp.measure("wt", a, now=NOW, dash={"now": NOW.isoformat(), "rows": []}, rows=rows,
                         bot={"started": NOW.timestamp() - 7200}, mult={"KXFOO-26NOV-T2": 1.25}, L=L)
        m = {x["ticker"]: x for x in res["markets"]}
        self.assertEqual((m["KXFOO-26NOV-T1"]["status"], m["KXFOO-26NOV-T2"]["status"]), ("judged", "restoring"))
        self.assertAlmostEqual(m["KXFOO-26NOV-T1"]["rate"], 0.41)                   # uptime from the change on
        plan = pp.plan_resize(res, {}, a, NOW)
        self.assertNotIn("KXFOO-26NOV-T2", plan["candidates"])
        self.assertEqual(plan["skipped"]["restoring"], 1)


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


J18 = {"pool": 17.57, "uptime": 1.0}


class Trim(unittest.TestCase):
    """TRIM (Jack 2026-11-18 on the FX relight: "dont overquote if unneeded to hit the target
    threshold"): a market over its target comes down to the size that target needs."""

    def test_the_cut_is_the_smallest_step_still_at_the_target(self):
        j = dict(J18, share=0.06)
        x = pp.trim_mult(j, 0.2, 74.0, 1.0, 1.20)
        self.assertEqual(x, 0.4)                              # needs ~0.385: rounds UP to the grid
        self.assertGreaterEqual(pp.share_at(x, 0.06), pp.TRIM_MIN_SHARE)
        self.assertGreaterEqual(pp.proj_at(x, j, 0.2, 74.0, 1.0), pp.TRIM_TO * 1.20)
        self.assertLess(pp.proj_at(x - pp.TRIM_GRID, j, 0.2, 74.0, 1.0), pp.TRIM_TO * 1.20)

    def test_the_whole_share_loss_counts(self):
        # others standing still: 6% at 1x -> 2.49% at 0.4x (not 2.4%: our size is part of the level)
        self.assertAlmostEqual(pp.share_at(0.4, 0.06), 0.024 / 0.964)

    def test_one_step_leaves_a_quarter_and_never_goes_under_the_floor(self):
        self.assertEqual(pp.trim_mult(dict(J18, share=0.18), 0.3, 74.0, 4.0, 1.20), 1.0)   # needs ~0.42
        self.assertEqual(pp.trim_mult(dict(J18, share=0.5), 0.0, 74.0, 0.5, 1.20), 0.2)
        self.assertIsNone(pp.trim_mult(dict(J18, share=0.5), 0.0, 74.0, 0.2, 1.20))       # at the floor

    def test_none_inside_the_trigger_under_the_target_or_with_no_share(self):
        self.assertIsNone(pp.trim_mult(dict(J18, share=0.025), 0.2, 74.0, 1.0, 1.20))     # needs ~0.96 of it
        self.assertIsNone(pp.trim_mult(dict(J18, share=0.012), 0.2, 74.0, 1.0, 1.20))     # under the target
        self.assertIsNone(pp.trim_mult(dict(J18, share=0.0), 2.0, 74.0, 1.0, 1.20))

    def test_banked_money_lowers_the_need_to_the_share_floor(self):
        j = dict(J18, share=0.06)
        self.assertEqual(pp.trim_mult(j, 0.0, 74.0, 1.0, 1.20), 0.45)      # $1.50 still to earn
        x = pp.trim_mult(j, 1.6, 10.0, 1.0, 1.20)                          # $1.60 banked
        self.assertEqual(x, 0.4)                                           # Jack's 2% share (+ cushion) binds
        self.assertGreaterEqual(pp.share_at(x, 0.06), pp.TRIM_MIN_SHARE)


def _mt(t, xt, m0, stop="2026-11-22T05:00:00Z", proj=3.0, **kw):
    m = _m(t, proj, m0=m0, **kw)
    m.update(x_trim=xt, stop=stop, period_start="2026-11-18T19:01:00Z")
    return m


class TrimPlan(unittest.TestCase):
    A = argparse.Namespace(aim=1.20, max_mult=5.0, keep_within=0.05, no_off=False, trim=True)

    def _prev(self, cands):
        return {"at": (NOW - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"), "trim_candidates": cands}

    def test_two_reads_at_the_same_multiple(self):
        p = pp.plan_resize({"markets": [_mt("KXA-1", 0.4, 1.0)]}, self._prev({"KXA-1": 1.0}), self.A, NOW)
        self.assertEqual((p["down"], p["down_next"], p["trim_candidates"]), ({"KXA-1": 0.4}, [], {"KXA-1": 1.0}))
        for prev in (self._prev({"KXA-1": 2.0}), {}):        # the multiple moved since / a first read
            p = pp.plan_resize({"markets": [_mt("KXA-1", 0.4, 1.0)]}, prev, self.A, NOW)
            self.assertEqual((p["down"], p["down_next"]), ({}, ["KXA-1"]))

    def test_off_by_default_and_nothing_under_the_aim_trims(self):
        a = argparse.Namespace(aim=1.20, max_mult=5.0, keep_within=0.05, no_off=False)
        p = pp.plan_resize({"markets": [_mt("KXA-1", 0.4, 1.0)]}, self._prev({"KXA-1": 1.0}), a, NOW)
        self.assertEqual((p["down"], p["down_next"], p["keep"]), ({}, [], 1))
        p = pp.plan_resize({"markets": [_mt("KXA-1", 0.4, 1.0, proj=1.1, x_aim=1.25)]}, self._prev({"KXA-1": 1.0}),
                           self.A, NOW)
        self.assertEqual((p["down"], p["up"]), ({}, {"KXA-1": 1.25}))


class TrimApply(unittest.TestCase):
    S, S2, P = "2026-11-22T05:00:00Z", "2026-11-29T05:00:00Z", "2026-11-18T19:01:00Z"

    def setUp(self):
        self.mult = os.path.join(TMP, "size_mult_tr.json")
        self.state = os.path.join(TMP, "resize_state_tr.json")
        self.trims = os.path.join(TMP, "trim_tr.json")
        for p in (self.mult, self.state, self.trims):
            if os.path.exists(p):
                os.remove(p)
        self.saved = (pp.measure, pp.print_pace, pp.size_mult_path, pp.resize_state_path, pp.trim_state_path,
                      pp.subprocess.run)
        pp.print_pace = lambda r: None
        pp.size_mult_path = lambda n: self.mult
        pp.resize_state_path = lambda n: self.state
        pp.trim_state_path = lambda n: self.trims
        pp.subprocess.run = lambda *a, **k: None

    def tearDown(self):
        (pp.measure, pp.print_pace, pp.size_mult_path, pp.resize_state_path, pp.trim_state_path,
         pp.subprocess.run) = self.saved

    def _write(self, path, d):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh)

    def _rec(self, frm, to, stop=None):
        return {"from": frm, "to": to, "at": "x", "stop": stop or self.S, "period_start": self.P}

    def _run(self, markets, trim=True, prev_trim=None):
        now = datetime.now(timezone.utc)
        if prev_trim is not None:
            self._write(self.state, {"at": (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     "off_candidates": [], "trim_candidates": prev_trim})
        res = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "since_restart_min": 200.0, "dash_age_min": 2.0,
               "markets": markets}
        pp.measure = lambda name, a, now=None: res
        a = argparse.Namespace(name="tr", aim=1.20, max_mult=5.0, real=0.65, window_min=60.0, keep_within=0.05,
                               no_off=False, json=None, apply=True, force=False, no_run=True, trim=trim)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(pp.cmd_resize(a), 0)
        with open(self.mult, encoding="utf-8") as fh:
            mult = json.load(fh)
        rec = {}
        if os.path.exists(self.trims):
            with open(self.trims, encoding="utf-8") as fh:
                rec = json.load(fh)
        return mult, rec

    def test_a_second_read_trims_and_keeps_the_earlier_multiple(self):
        self._write(self.mult, {"KXA-1": 2.0})
        mult, rec = self._run([_mt("KXA-1", 0.5, 2.0), _mt("KXA-2", 0.4, 1.0)], prev_trim={"KXA-1": 2.0})
        self.assertEqual(mult, {"KXA-1": 0.5})                # KXA-2 on its first read: unchanged
        self.assertEqual((rec["KXA-1"]["from"], rec["KXA-1"]["to"], rec["KXA-1"]["stop"]), (2.0, 0.5, self.S))
        # a further step keeps the first "from"
        mult, rec = self._run([_mt("KXA-1", 0.25, 0.5)], prev_trim={"KXA-1": 0.5})
        self.assertEqual((mult["KXA-1"], rec["KXA-1"]["from"]), (0.25, 2.0))

    def test_turning_it_off_gives_the_trims_back(self):
        self._write(self.mult, {"KXA-1": 0.5})
        self._write(self.trims, {"KXA-1": self._rec(2.0, 0.5)})
        mult, rec = self._run([_mt("KXA-1", 0.4, 0.5)], trim=False, prev_trim={"KXA-1": 0.5})
        self.assertEqual((mult, rec), ({"KXA-1": 2.0}, {}))

    def test_a_relight_or_a_market_off_the_list_gets_it_back(self):
        self._write(self.mult, {"KXA-1": 0.5, "KXA-2": 0.4})
        self._write(self.trims, {"KXA-1": self._rec(2.0, 0.5), "KXA-2": self._rec(1.0, 0.4)})
        mult, rec = self._run([_mt("KXA-1", None, 0.5, stop=self.S2)])    # a new period; KXA-2 not listed
        self.assertEqual((mult, rec), ({"KXA-1": 2.0, "KXA-2": 1.0}, {}))

    def test_a_size_up_past_it_drops_the_record_and_an_off_stays_off(self):
        self._write(self.mult, {"KXA-1": 0.5, "KXA-2": 0})
        self._write(self.trims, {"KXA-1": self._rec(1.0, 0.5), "KXA-2": self._rec(2.0, 0.5)})
        mult, rec = self._run([_mt("KXA-1", None, 0.5, proj=0.9, x_aim=1.25), _mt("KXA-2", None, 0, status="off")])
        self.assertEqual((mult, rec), ({"KXA-1": 1.25, "KXA-2": 0}, {}))


def _mr(t, proj, costs, x_aim=None, proj_max=0.0, m0=1.0, status="judged", pre_off=None,
        stop="2026-11-22T05:00:00Z"):
    m = _m(t, proj, x_aim=x_aim, proj_max=proj_max, m0=m0, status=status)
    m.update(costs=costs, stop=stop, period_start="2026-11-18T19:01:00Z")
    if pre_off is not None:
        m["pre_off"] = pre_off
    return m


class ReachPlan(unittest.TestCase):
    """TIER REACH (Jack 2026-11-19, the FX walls past the 15c tier: "Up to 25c on cheap tier
    multiplier if needed"): the last tier out to 25c, a market at a time, only where the
    multiple can't take it to the aim."""
    A = argparse.Namespace(aim=1.20, max_mult=5.0, keep_within=0.05, no_off=False, reach_to=25.0, reach_lo=15.0)
    PREV = {"at": (NOW - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"), "off_candidates": ["KXA-2"]}

    def _markets(self):
        return [_mr("KXA-1", 1.1, {"bid": 21.0}, proj_max=1.1, m0=5.0),               # at the cap, short of the aim
                _mr("KXA-2", 0.3, {"bid": 20.0, "ask": 4.0}, proj_max=0.5, m0=3.0),     # under $1 even at 5x
                _mr("KXA-3", 0.3, {"bid": 27.0, "ask": 4.0}, proj_max=0.5),             # its wall past 25c
                _mr("KXA-4", 0.3, {"bid": 12.0}, proj_max=0.5),                         # inside the tiers already
                _mr("KXA-5", 0.0, {"bid": 21.0}, m0=0, status="off", pre_off=True),     # off: back at 1x
                _mr("KXA-6", 0.0, {"bid": 21.0}, m0=0, status="off", pre_off=False),    # no sizes to bring back
                _mr("KXA-7", 0.0, {"bid": 80.0}, m0=0, status="off", pre_off=True),     # at the money: no reach
                _mr("KXA-8", 0.6, {"bid": 21.0}, x_aim=2.5, proj_max=1.9),              # the multiple goes first
                _mr("KXA-9", 0.6, {"bid": 21.0}, proj_max=1.05, m0=3.0)]                # ...as far as the cap

    def test_only_where_the_multiple_runs_out(self):
        p = pp.plan_resize({"markets": self._markets()}, self.PREV, self.A, NOW)
        self.assertEqual(set(p["reach"]), {"KXA-1", "KXA-2"})
        self.assertIn("at the 5x cap, short of the $1.20 aim", p["reach"]["KXA-1"])
        self.assertIn("under $1 even at 5x", p["reach"]["KXA-2"])
        self.assertEqual(p["reach_restore"], {"KXA-5": 1.0})
        self.assertEqual(p["up"], {"KXA-8": 2.5, "KXA-9": 5.0})
        self.assertEqual((p["off"], sorted(p["off_next"])), ([], ["KXA-3", "KXA-4"]))   # KXA-2: not off
        self.assertEqual(p["skipped"]["off"], 2)

    def test_once_a_market_and_none_when_off(self):
        a = argparse.Namespace(**vars(self.A))
        a.reach_granted = {"KXA-1", "KXA-2", "KXA-5"}
        p = pp.plan_resize({"markets": self._markets()}, self.PREV, a, NOW)
        self.assertEqual((p["reach"], p["reach_restore"]), ({}, {}))
        self.assertEqual(p["off"], ["KXA-2"])                 # with its reach, under $1 on two reads: off
        self.assertEqual(p["at_max"], 1)
        self.assertEqual(p["skipped"]["off"], 3)
        for to, lo in ((0.0, 15.0), (30.0, 15.0), (25.0, None), (15.0, 15.0)):   # off / past 25c / no tier
            a = argparse.Namespace(**dict(vars(self.A), reach_to=to, reach_lo=lo))
            p = pp.plan_resize({"markets": self._markets()}, self.PREV, a, NOW)
            self.assertEqual((p["reach"], p["reach_restore"]), ({}, {}), (to, lo))
        a = argparse.Namespace(aim=1.20, max_mult=5.0, keep_within=0.05, no_off=False)   # no reach args at all
        self.assertEqual(pp.plan_resize({"markets": self._markets()}, self.PREV, a, NOW)["reach"], {})

    def test_last_tier_and_reach_sides(self):
        self.assertEqual(pp.last_tier(["--cheap-tiers", "2:10,5:5,15:5"]), (15.0, 5.0))
        self.assertEqual(pp.last_tier(["--cheap-tiers", "15:5,2:10"]), (15.0, 5.0))
        self.assertIsNone(pp.last_tier(["--room", "3"]))
        self.assertEqual(pp.reach_sides({"bid": 21.0, "ask": 15.0}, 15.0, 25.0), ["bid"])
        self.assertEqual(pp.reach_sides({"bid": 25.0, "ask": 25.5}, 15.0, 25.0), ["bid"])
        self.assertEqual(pp.reach_sides(None, 15.0, 25.0), [])


class ReachApply(unittest.TestCase):
    S, S2, P = "2026-11-22T05:00:00Z", "2026-11-29T05:00:00Z", "2026-11-18T19:01:00Z"

    def setUp(self):
        self.mult = os.path.join(TMP, "size_mult_ra.json")
        self.state = os.path.join(TMP, "resize_state_ra.json")
        self.tiers = os.path.join(TMP, "tier_ra.json")
        for p in (self.mult, self.state, self.tiers):
            if os.path.exists(p):
                os.remove(p)
        self.saved = (pp.measure, pp.print_pace, pp.size_mult_path, pp.resize_state_path, pp.tier_state_path,
                      pp.subprocess.run)
        pp.print_pace = lambda r: None
        pp.size_mult_path = lambda n: self.mult
        pp.resize_state_path = lambda n: self.state
        pp.tier_state_path = lambda n: self.tiers
        pp.subprocess.run = lambda *a, **k: None

    def tearDown(self):
        (pp.measure, pp.print_pace, pp.size_mult_path, pp.resize_state_path, pp.tier_state_path,
         pp.subprocess.run) = self.saved

    def _write(self, path, d):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh)

    def _run(self, markets, reach_to=25.0, prev_off=None):
        now = datetime.now(timezone.utc)
        if prev_off is not None:
            self._write(self.state, {"at": (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     "off_candidates": prev_off})
        res = {"at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "since_restart_min": 200.0, "dash_age_min": 2.0,
               "markets": markets}
        pp.measure = lambda name, a, now=None: res
        a = argparse.Namespace(name="ra", aim=1.20, max_mult=5.0, real=0.65, window_min=60.0, keep_within=0.05,
                               no_off=False, json=None, apply=True, force=False, no_run=True, trim=False,
                               reach_to=reach_to, reach_lo=15.0)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(pp.cmd_resize(a), 0)
        with open(self.mult, encoding="utf-8") as fh:
            mult = json.load(fh)
        d = {}
        if os.path.exists(self.tiers):
            with open(self.tiers, encoding="utf-8") as fh:
                d = json.load(fh)
        return mult, d, out.getvalue()

    def test_given_and_restored_with_a_record_each(self):
        self._write(self.mult, {"KXA-1": 5.0, "KXA-5": 0})
        mult, d, out = self._run([_mr("KXA-1", 1.1, {"bid": 21.0}, proj_max=1.1, m0=5.0),
                                  _mr("KXA-5", 0.0, {"bid": 22.0, "ask": 3.0}, m0=0, status="off", pre_off=True)])
        self.assertEqual(mult, {"KXA-1": 5.0, "KXA-5": 1.0})         # the reach keeps KXA-1's multiple
        self.assertEqual(d["to"], 25.0)
        self.assertEqual(sorted(d["markets"]), ["KXA-1", "KXA-5"])
        r1, r5 = d["markets"]["KXA-1"], d["markets"]["KXA-5"]
        self.assertEqual((r1["sides"], r1["stop"], r1["period_start"]), (["bid"], self.S, self.P))
        self.assertEqual((r5["why"], r5["restored"], r5["sides"]), ("back from off with the reach", 1.0, ["bid"]))
        self.assertIn("tier reach: 1 given, 1 back from off, 0 dropped", out)

    def test_one_reach_a_market_then_off_on_two_reads_and_never_back(self):
        self._write(self.mult, {"KXA-2": 3.0})
        m = _mr("KXA-2", 0.3, {"bid": 20.0}, proj_max=0.5, m0=3.0)
        mult, d, _ = self._run([m])
        self.assertEqual((mult, sorted(d["markets"])), ({"KXA-2": 3.0}, ["KXA-2"]))
        mult, d, _ = self._run([m], prev_off=[])                      # still short with it: first read
        mult, d, _ = self._run([m], prev_off=["KXA-2"])               # second read: off
        self.assertEqual(mult, {"KXA-2": 0})
        self.assertIn("KXA-2", d["markets"])                          # the record stays...
        mult, d, _ = self._run([_mr("KXA-2", 0.0, {"bid": 20.0}, m0=0, status="off", pre_off=True)])
        self.assertEqual(mult, {"KXA-2": 0})                          # ...so it isn't brought back

    def test_off_drops_every_reach_and_a_relight_or_leaving_drops_one(self):
        self._write(self.mult, {"KXA-1": 5.0, "KXA-5": 1.0, "KXA-9": 2.0})
        self._write(self.tiers, {"to": 25.0, "markets": {
            t: {"at": "x", "why": "y", "sides": ["bid"], "stop": self.S, "period_start": self.P}
            for t in ("KXA-1", "KXA-5", "KXA-9")}})
        mult, d, _ = self._run([_mr("KXA-1", 1.5, {"bid": 21.0}, m0=5.0),
                                _mr("KXA-5", 1.5, {"bid": 21.0}, stop=self.S2)])   # KXA-5 relit; KXA-9 gone
        self.assertEqual(sorted(d["markets"]), ["KXA-1"])
        mult, d, out = self._run([_mr("KXA-1", 1.5, {"bid": 21.0}, m0=5.0)], reach_to=0.0)
        self.assertEqual(d, {"to": 0.0, "markets": {}})
        self.assertEqual(mult["KXA-1"], 5.0)                          # its multiple stays; judged without it
        self.assertIn("tier reach: 0 given, 0 back from off, 1 dropped", out)

    def test_nothing_written_on_a_wave_that_never_had_one(self):
        self._write(self.mult, {"KXA-1": 2.0})
        mult, d, out = self._run([_mr("KXA-1", 1.5, {"bid": 21.0}, m0=2.0)], reach_to=0.0)
        self.assertEqual((mult, d), ({"KXA-1": 2.0}, {}))
        self.assertNotIn("tier reach", out)


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
            fh.write(f"{s(3)} [IMM] fx-event stand-aside T-6: KXUSDMXNEVENT source price is 240s old\n")
            fh.write(f"{s(3)} [IMM] fx-event stand-aside T-7: KXGOLDEVENT jump hold (4m left)\n")
            fh.write(f"{s(2)} [IMM] fx-event resume T-7\n")
        got = pp.log_side_reasons(["T-1", "T-2", "T-3", "T-4", "T-5", "T-6", "T-7"], now)
        self.assertIn("FX fair stands it aside (KXUSDMXNEVENT source price", got[("T-6", "bid")])
        self.assertNotIn(("T-7", "ask"), got)             # resumed
        self.assertIn(("T-1", "ask"), got)
        self.assertIn(("T-2", "bid"), got)
        self.assertNotIn(("T-3", "ask"), got)             # dry: logged only
        self.assertIn(("T-4", "bid"), got)
        self.assertIn(("T-4", "ask"), got)
        self.assertNotIn(("T-5", "bid"), got)             # cleared
        os.remove(p)


class FloorPass(unittest.TestCase):
    """The $1-floor check in every 15-min pass (Jack 2026-11-14: "Yes, every 15 min")."""

    def setUp(self):
        self.mult = os.path.join(TMP, "size_mult_wt.json")
        self.fair = os.path.join(TMP, "wave_fair_wt.json")
        self._fair(NOW + timedelta(minutes=30))
        self.w = _wave(tune=list(pw.PRESETS["default"]) + ["--size-mult-file", self.mult, "--fair-file", self.fair],
                       floor_auto=True)
        self.calls = []
        self.real = pp.cmd_resize
        pp.cmd_resize = lambda a: self.calls.append(a) or 0

    def tearDown(self):
        pp.cmd_resize = self.real

    def _fair(self, valid_until):
        with open(self.fair, "w", encoding="utf-8") as fh:
            json.dump({"at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "valid_until": valid_until.strftime("%Y-%m-%dT%H:%M:%SZ")}, fh)

    def test_applies_without_its_own_run_and_never_above_5x(self):
        self.w["floor"] = {"max_mult": 10}
        with redirect_stdout(io.StringIO()):
            pp.pass_resize("wt", self.w, now=NOW)
        a = self.calls[0]
        self.assertEqual((a.name, a.apply, a.no_run, a.force, a.no_off), ("wt", True, True, False, False))
        self.assertEqual((a.aim, a.max_mult, a.trim), (1.20, 5.0, False))      # no trim unless the wave turns it on
        self.w["floor"] = {"trim": True}
        with redirect_stdout(io.StringIO()):
            pp.pass_resize("wt", self.w, now=NOW)
        self.assertTrue(self.calls[1].trim)

    def test_floor_trim_needs_the_floor_check(self):
        _save_reg({"wt": _wave()})
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            pw._set(argparse.Namespace(name="wt", key="floor_trim", value="on"))
        with redirect_stdout(io.StringIO()):
            pw._set(argparse.Namespace(name="wt", key="floor_auto", value="on"))
            pw._set(argparse.Namespace(name="wt", key="floor_trim", value="on"))
        self.assertEqual(pw.load()["waves"]["wt"]["floor"], {"trim": True})
        self.assertTrue(pp.wave_trim_on("wt"))
        with redirect_stdout(io.StringIO()):
            pw._set(argparse.Namespace(name="wt", key="floor_trim", value="off"))
        self.assertFalse(pp.wave_trim_on("wt"))
        self.assertFalse(pp.wave_trim_on("no-such-wave"))

    def test_floor_tier_needs_the_floor_check_a_tier_and_stays_within_25c(self):
        """Jack 2026-11-19: "Up to 25c on cheap tier multiplier if needed" """
        tune = list(pw.PRESETS["default"])
        tune[tune.index("--cheap-tiers") + 1] = "2:10,5:5,15:5"
        _save_reg({"wt": _wave(tune=tune)})
        for v in ("25", ):
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                pw._set(argparse.Namespace(name="wt", key="floor_tier", value=v))    # no floor check yet
        with redirect_stdout(io.StringIO()):
            pw._set(argparse.Namespace(name="wt", key="floor_auto", value="on"))
        for v in ("26", "15", "10"):                                  # past Jack's 25c / not past the tier
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
                pw._set(argparse.Namespace(name="wt", key="floor_tier", value=v))
        with redirect_stdout(io.StringIO()):
            pw._set(argparse.Namespace(name="wt", key="floor_tier", value="25"))
            pw._set(argparse.Namespace(name="wt", key="floor_tier", value="25"))    # the flag added once
        w = pw.load()["waves"]["wt"]
        self.assertEqual(w["floor"]["tier_to"], 25.0)
        self.assertEqual(pw._vals(w["tune"], "--tier-file"), [pp.tier_state_path("wt")])
        self.assertEqual(pp.wave_reach("wt"), (25.0, 15.0))
        checks = {k: (st, msg) for st, k, msg in pw.wave_checks("wt", w)}
        self.assertEqual(checks["wt main floor tier reach"][0], pw.WARN)
        with redirect_stdout(io.StringIO()):
            pp.pass_resize("wt", dict(w, tune=w["tune"] + ["--fair-file", self.fair]), now=NOW)
        self.assertEqual((self.calls[-1].reach_to, self.calls[-1].reach_lo), (25.0, 15.0))
        with redirect_stdout(io.StringIO()):
            pw._set(argparse.Namespace(name="wt", key="floor_tier", value="off"))
        w = pw.load()["waves"]["wt"]
        self.assertEqual((w["floor"]["tier_to"], pw._vals(w["tune"], "--tier-file")), (0, []))
        self.assertEqual(pp.wave_reach("wt"), (0.0, 15.0))
        self.assertNotIn("wt main floor tier reach", {k for _st, k, _m in pw.wave_checks("wt", w)})
        with redirect_stdout(io.StringIO()):
            pp.pass_resize("wt", dict(w, tune=w["tune"] + ["--fair-file", self.fair]), now=NOW)
        self.assertEqual(self.calls[-1].reach_to, 0.0)
        _save_reg({"wt": _wave(floor_auto=True)})                     # the default preset: 2:10,5:5
        with redirect_stdout(io.StringIO()):
            pw._set(argparse.Namespace(name="wt", key="floor_tier", value="25"))
        self.assertEqual(pp.wave_reach("wt"), (25.0, 5.0))

    def test_skips_without_a_live_fair_or_a_multiples_file(self):
        self._fair(NOW - timedelta(minutes=1))
        out = io.StringIO()
        with redirect_stdout(out):
            pp.pass_resize("wt", self.w, now=NOW)
            os.remove(self.fair)
            pp.pass_resize("wt", self.w, now=NOW)
            pp.pass_resize("wt", _wave(tune=list(pw.PRESETS["default"]) + ["--fair-file", self.fair]), now=NOW)
        self.assertEqual(self.calls, [])
        self.assertIn("fair file stale", out.getvalue())
        self.assertIn("fair file unreadable", out.getvalue())
        self.assertIn("no --size-mult-file", out.getvalue())

    def test_a_wave_the_bot_prices_counts_as_live(self):
        """Jack 2026-11-15: "build these to generalize into the market-wave skill" -- the
        crypto wave has no fair file; its fair is the bot's crypto_event_fair.json"""
        st = os.path.join(INC, "crypto_event_fair.json")

        def bot(age_min, series="KXBTCEVENT"):
            with open(st, "w", encoding="utf-8") as fh:
                json.dump({"checked_ts": NOW.timestamp() - age_min * 60,
                           "markets": {f"{series}-17NOV260930-T7000000": {"p": 0.4}}}, fh)
        w = _wave(series=["KXBTCEVENT"], tune=list(pw.PRESETS["default"]) + ["--size-mult-file", self.mult],
                  floor_auto=True)
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                bot(1.0)
                pp.pass_resize("wcr", w, now=NOW)
                self.assertEqual(len(self.calls), 1)
                bot(9.0)                                      # the bot's read stopped
                pp.pass_resize("wcr", w, now=NOW)
                bot(1.0, series="KXETHEVENT")                 # it prices other series only
                pp.pass_resize("wcr", w, now=NOW)
        finally:
            os.remove(st)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("the bot's crypto_event_fair.json is 9 min old", out.getvalue())
        self.assertIn("the bot prices none of its series live", out.getvalue())

    def test_floor_auto_on_adds_the_multiples_file_and_max_mult_caps_it(self):
        _save_reg({"wt": _wave()})
        with redirect_stdout(io.StringIO()):
            pw._set(argparse.Namespace(name="wt", key="floor_auto", value="on"))
            pw._set(argparse.Namespace(name="wt", key="floor_auto", value="on"))   # added once
            pw._set(argparse.Namespace(name="wt", key="floor_max_mult", value="1"))
        w = pw.load()["waves"]["wt"]
        self.assertTrue(w["floor_auto"])
        self.assertEqual(pw._vals(w["tune"], "--size-mult-file"), [pp.size_mult_path("wt")])
        self.assertEqual(w["floor"], {"max_mult": 1.0})
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            pw._set(argparse.Namespace(name="wt", key="floor_max_mult", value="6"))
        w["floor"] = {"max_mult": 1.0}
        with redirect_stdout(io.StringIO()):
            pp.pass_resize("wt", dict(w, tune=w["tune"] + ["--fair-file", self.fair]), now=NOW)
        self.assertEqual(self.calls[-1].max_mult, 1.0)               # only offs

    def test_the_pass_runs_it_after_the_fair_and_before_the_tune_only_when_on(self):
        order = []
        real_one, real_load = pw.run_one, pw.load
        pw.run_one = lambda cmd, env=None, tail=4: order.append(
            "tune" if any("pvd_wave_tune" in str(x) for x in cmd) else "scan") or 0
        pp_pass = pp.pass_resize
        pp.pass_resize = lambda name, w, now=None: order.append("resize") or 0
        try:
            with redirect_stdout(io.StringIO()):
                pw._run_wave("wt", self.w, argparse.Namespace(write=True))
                self.assertEqual(order, ["scan", "resize", "tune"])
                order.clear()
                pw._run_wave("wt", dict(self.w, floor_auto=False), argparse.Namespace(write=True))
                self.assertEqual(order, ["scan", "tune"])
                order.clear()
                pw._run_wave("wt", self.w, argparse.Namespace(write=False))   # a dry run never resizes
                self.assertEqual(order, ["scan", "tune"])
        finally:
            pw.run_one, pw.load, pp.pass_resize = real_one, real_load, pp_pass


class ResizeNoRun(unittest.TestCase):
    def test_no_run_writes_the_multiples_but_no_setarg_or_wave_run(self):
        mult = os.path.join(TMP, "size_mult_nr.json")
        state = os.path.join(TMP, "resize_state_nr.json")
        with open(state, "w", encoding="utf-8") as fh:
            json.dump({"at": (datetime.now(timezone.utc) - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "off_candidates": ["KXA-6"]}, fh)
        res = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "since_restart_min": 200.0,
               "dash_age_min": 2.0,
               "markets": [_m("KXA-2", 0.6, x_aim=2.5, proj_max=1.9), _m("KXA-6", 0.1, proj_max=0.3)]}
        saved = (pp.measure, pp.print_pace, pp.size_mult_path, pp.resize_state_path, pp.subprocess.run)
        runs = []
        pp.measure = lambda name, a, now=None: res
        pp.print_pace = lambda r: None
        pp.size_mult_path = lambda n: mult
        pp.resize_state_path = lambda n: state
        pp.subprocess.run = lambda *a, **k: runs.append(a)
        try:
            a = argparse.Namespace(name="nr", aim=1.20, max_mult=5.0, real=0.65, window_min=60.0, keep_within=0.05,
                                   no_off=False, json=None, apply=True, force=False, no_run=True)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(pp.cmd_resize(a), 0)
        finally:
            pp.measure, pp.print_pace, pp.size_mult_path, pp.resize_state_path, pp.subprocess.run = saved
        with open(mult, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"KXA-2": 2.5, "KXA-6": 0})   # up; off on its 2nd read
        self.assertEqual(runs, [])


if __name__ == "__main__":
    unittest.main()
