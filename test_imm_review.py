"""Tests for imm_review.py and send_imm_review.py -- the IMM daily review's
deterministic half. Every test builds a synthetic run-logs tree in a temp dir
(configure() points the module there); nothing reads or writes the live tree,
calls Kalshi, PowerShell or git, or sends mail."""

import json
import math
import os
import pickle
import shutil
import tempfile
import unittest
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import imm_review as rv
import send_imm_review as sr

DAY = date(2026, 11, 24)


def _ts(day: date, hh=12, mm=0) -> float:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=rv.ET).timestamp()


def _row(**kw):
    v = {k: 0 for k in rv.DAY_FIELDS}
    v["settled"] = 0
    fx = kw.pop("fx", None)
    v.update(kw)
    v["fx"] = fx if fx is not None else 0
    return [v[k] for k in rv.DAY_FIELDS]


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.status = os.path.join(self.root, "run-logs", "incentive-mm")
        self.review = os.path.join(self.status, "daily_review")
        os.makedirs(os.path.join(self.status, "dashboard", "cache", "day_archive"))
        rv.configure(status_dir=self.status, review_dir=self.review, repo=self.root)
        self._fam = (rv._FAM_FN, dict(rv._FAM_CACHE), rv._META)
        rv._FAM_CACHE.clear()
        rv._FAM_FN = lambda s: (("Fam " + s[:4]), s)
        rv._META = {}

    def tearDown(self):
        rv._FAM_FN, cache, rv._META = self._fam
        rv._FAM_CACHE.clear()
        rv._FAM_CACHE.update(cache)
        rv.configure()
        shutil.rmtree(self.root, ignore_errors=True)

    # helpers
    def archive(self, day: date, rows: dict, ok=True):
        s, e = rv.et_bounds(day)
        blob = {"v": 1, "s": s, "e": e, "ok": ok, "f": list(rv.DAY_FIELDS), "fx": list(rv.FX_KEYS),
                "m": rows}
        with open(os.path.join(rv.ARCHIVE_DIR, day.isoformat() + ".json"), "w") as fh:
            json.dump(blob, fh)

    def write(self, rel, text, mode="w"):
        p = os.path.join(self.status, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, mode, encoding="utf-8") as fh:
            fh.write(text)
        return p

    def jsonl(self, rel, rows):
        return self.write(rel, "".join(json.dumps(r) + "\n" for r in rows))


class ScorecardTests(Base):
    def setUp(self):
        super().setUp()
        # three days: net 100, -50, 30
        self.archive(DAY - timedelta(days=2), {"KXAAA-1-T1": _row(rew=120, pnl=-20, rest=1000)})
        self.archive(DAY - timedelta(days=1), {"KXAAA-1-T1": _row(rew=10, pnl=-60, rest=1000)})
        self.archive(DAY, {"KXAAA-1-T1": _row(rew=50, pnl=-10, fills=2, cts=40, rest=2000,
                                              fx=[1, 40, -2, 40, -4, 40, -6, 40]),
                           "KXBBB-2-T5": _row(rew=0, pnl=-10, pos1=-5)})
        self.write("dashboard/imm_dashboard_summary.json", json.dumps(
            {"generated": 1.0, "days": [{"d": DAY.isoformat(), "rew": 50.0, "pnl": -20.0}],
             "windows": {"today": {"rew": 5, "pnl": -1, "fills": 1}}}))
        self.write("reward_credits.csv", "credit_date,event_ticker,amount,kind\n"
                                         "2026-11-18,X-1,2.00,Liquidity\n2026-11-15,X-2,3.00,Liquidity\n"
                                         "2026-03-21,X-3,0.31,Volume\n")

    def test_day_totals_and_windows(self):
        sc = rv.sec_scorecard(DAY)
        d = sc["day_totals"]
        self.assertAlmostEqual(d["rew"], 50)
        self.assertAlmostEqual(d["pnl"], -20)
        self.assertAlmostEqual(d["net"], 30)
        self.assertEqual(d["markets"], 2)
        self.assertEqual(d["markets_rew"], 1)
        self.assertAlmostEqual(d["inv_cts"], 5)
        w = sc["7d"]
        self.assertEqual(w["n"], 3)
        self.assertAlmostEqual(w["mean"], round(80 / 3, 2))
        self.assertAlmostEqual(w["max_dd"], -50.0)
        self.assertEqual(w["worst"], -50.0)
        self.assertEqual(w["green"], 2)
        sd = math.sqrt(sum((x - 80 / 3) ** 2 for x in (100, -50, 30)) / 3)
        self.assertAlmostEqual(w["sharpe"], round((80 / 3) / sd * math.sqrt(365), 2))
        self.assertAlmostEqual(w["tax"], round(100 / 180, 3))   # trading -100 against rewards 180
        self.assertEqual(sc["dash_check"]["diff_rew"], 0.0)
        self.assertEqual(sc["dash_check"]["diff_pnl"], 0.0)
        self.assertEqual(sc["credits"]["through"], "2026-11-18")
        self.assertEqual(sc["credits"]["age_days"], 7)
        self.assertAlmostEqual(sc["credits"]["last7_credited"], 5.0)

    def test_not_ok_days_are_left_out_of_windows(self):
        self.archive(DAY - timedelta(days=3), {"KXAAA-1-T1": _row(rew=999)}, ok=False)
        sc = rv.sec_scorecard(DAY)
        self.assertEqual(sc["inception"]["n"], 3)

    def test_totals_cache_is_reused_and_refreshed(self):
        rv.all_day_totals()
        self.assertTrue(os.path.exists(os.path.join(self.review, "archive_totals_cache.json")))
        self.archive(DAY, {"KXAAA-1-T1": _row(rew=1, pnl=1)})
        os.utime(os.path.join(rv.ARCHIVE_DIR, DAY.isoformat() + ".json"),
                 (1e9 + 5, 1e9 + 5))
        self.assertAlmostEqual(rv.all_day_totals()[DAY.isoformat()]["net"], 2)

    def test_scorecard_csv_upserts(self):
        sc = rv.sec_scorecard(DAY)
        rv.append_scorecard(DAY, sc)
        rv.append_scorecard(DAY, sc)
        with open(os.path.join(self.review, "scorecard.csv")) as fh:
            lines = fh.read().strip().splitlines()
        self.assertEqual(len(lines), 2)


class AttributionTests(Base):
    def test_families_events_and_losers(self):
        for i in range(8):
            d = DAY - timedelta(days=i)
            self.archive(d, {"KXLOSE-26NOV-T1": _row(rew=1, pnl=-10, fills=1, cts=10,
                                                     fx=[0, 10, -1, 10, -2, 10, -50, 10]),
                             "KXWINS-26NOV-T2": _row(rew=20, pnl=1)})
        at = rv.sec_attribution(DAY)
        fams = {f["family"]: f for f in at["families"]}
        self.assertAlmostEqual(fams["Fam KXLO"]["day"]["net"], -9)
        self.assertAlmostEqual(fams["Fam KXLO"]["d7"]["net"], -63)
        self.assertAlmostEqual(fams["Fam KXLO"]["d30"]["net"], -72)
        self.assertEqual(fams["Fam KXLO"]["d30"]["mt_c"], -500.0)
        self.assertEqual(at["worst_events"][0]["event"], "KXLOSE-26NOV")
        self.assertEqual(at["persistent_losers"][0]["series"], "KXLOSE")
        self.assertEqual(at["top_series_30d"][0]["series"], "KXWINS")

    def test_git_log_parse_multiline_body(self):
        out = ("\x1eabc1234\x1f1795600000\x1fimm: fix the thing (review R-261124-03)\x1f"
               "First body line\nCo-Authored-By: X\n\x1d\n\nincentive_mm.py\ntest_incentive_mm.py\n"
               "\x1edef5678\x1f1795600100\x1fdocs only\x1f\x1d\n\nREADME.md\n")
        rows = rv.parse_git_log(out)
        self.assertEqual([r["sha"] for r in rows], ["abc1234", "def5678"])
        self.assertEqual(rows[0]["ids"], ["R-261124-03"])
        self.assertTrue(rows[0]["bot"])
        self.assertEqual(rows[0]["files"], 2)
        self.assertFalse(rows[1]["bot"])
        self.assertEqual(rows[1]["files"], 1)

    def test_env_diff_ignores_set_order(self):
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        rows = [{"ts": iso(_ts(DAY - timedelta(days=1), 22)), "run_id": "a", "config": {
                    "code:S": "frozenset({'KXA', 'KXB'})", "IMM_X": "1", "code:T": "frozenset({'KXA'})"}},
                {"ts": iso(_ts(DAY, 9)), "run_id": "b", "config": {
                    "code:S": "frozenset({'KXB', 'KXA'})", "IMM_X": "2", "code:T": "frozenset({'KXA', 'KXC'})"}}]
        for u in sorted({r["ts"][:10] for r in rows}):
            self.jsonl(f"config_history_{u}.jsonl", [r for r in rows if r["ts"][:10] == u])
        ch = {c["key"]: c for c in rv.config_runs(DAY)["env_changes"]}
        self.assertNotIn("code:S", ch)
        self.assertEqual((ch["IMM_X"]["old"], ch["IMM_X"]["new"]), ("1", "2"))
        self.assertEqual((ch["code:T"]["old"], ch["code:T"]["new"]), ("", "+KXC"))

    def test_event_of_prefers_markets_meta(self):
        rv._META = {"KXQ-26-T1.5": {"ev": "KXQ-26"}}
        self.assertEqual(rv.event_of("KXQ-26-T1.5"), "KXQ-26")
        self.assertEqual(rv.event_of("KXZ-26NOV-B3"), "KXZ-26NOV")


class FingerprintAndLogTests(Base):
    def test_fingerprint_normalises(self):
        a = rv.fingerprint("[IMM] ! 3 position(s) unmarkable (valued at cost): KXA-26-B1, KXB-27-T2")
        b = rv.fingerprint("[IMM] ! 9 position(s) unmarkable (valued at cost): KXC-28-X")
        self.assertEqual(a, b)
        p = rv.fingerprint(r"[IMM] ! state save failed: [WinError 5] Access is denied: 'C:\x\imm_state.json.tmp' -> 'C:\x\y'")
        self.assertIn("<PATH>", p)
        self.assertIn("<URL>", rv.fingerprint("read failed for url: https://x.y/z?q=1"))

    def _log(self, day, lines):
        name = f"incentive-mm-{day.isoformat()}.log"
        p = self.write(name, "\n".join(lines) + "\n")
        os.utime(p, None)
        return p

    def test_scan_buckets_by_et_day(self):
        z = lambda d, hh, mm: datetime.fromtimestamp(_ts(d, hh, mm), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        self._log(DAY, [
            z(DAY, 1, 0) + " [IMM] kalshi account: key abc",
            z(DAY, 1, 1) + " [IMM] ! balance read failed (HttpError(429 Too Many Requests)); floor check skipped",
            z(DAY, 2, 0) + " [IMM] ! balance read failed (HttpError(429 Too Many Requests)); floor check skipped",
            z(DAY, 3, 0) + " [IMM] ALERT [toxic_halt] KXA-26-T1 BID picked off 2x in 3h",
            z(DAY, 4, 0) + " [IMM] !! no cycle heartbeat for 600s -- hard-exiting",
            z(DAY, 5, 0) + " [IMM] source file changed on disk (synced deploy); clean exit at the safe window",
            "[create_order->V2] {noise}",
            z(DAY, 6, 0) + " [IMM] cycle",
            "Traceback (most recent call last):",
            '  File "x.py", line 1, in <module>',
            "ValueError: bad 12",
            z(DAY + timedelta(days=1), 3, 0) + " [IMM] ! next-day line",
        ])
        res = rv.scan_bot_logs([DAY])[DAY.isoformat()]
        fps = res["fp"]
        self.assertEqual(sum(n for fp, n in fps.items() if "balance read failed" in fp), 2)
        self.assertEqual(res["starts"], 1)
        self.assertEqual(res["exits"]["clean exit (code change)"], 1)
        self.assertEqual(res["exits"]["watchdog hard exit"], 1)
        self.assertTrue(any(fp.startswith("TRACEBACK ValueError") for fp in fps))
        self.assertTrue(any(fp.startswith("ALERT [toxic_halt]") for fp in fps))
        self.assertTrue(any(res["sev"][fp] == "!!" for fp in fps))
        self.assertFalse(any("next-day" in fp for fp in fps))

    def test_new_spike_gone(self):
        db = {"old": {"days": {(DAY - timedelta(days=i)).isoformat(): 2 for i in range(1, 8)},
                      "sev": "!", "first": "2026-11-01"},
              "spiky": {"days": {**{(DAY - timedelta(days=i)).isoformat(): 2 for i in range(1, 8)},
                                 DAY.isoformat(): 30}, "sev": "!", "first": "2026-11-01"},
              "fresh": {"days": {DAY.isoformat(): 1}, "sev": "!!", "first": DAY.isoformat(),
                        "example": "e"}}
        c = rv.classify_signatures(db, DAY)
        self.assertEqual([x["fp"] for x in c["new"]], ["fresh"])
        self.assertEqual([x["fp"] for x in c["spikes"]], ["spiky"])
        self.assertEqual([x["fp"] for x in c["gone"]], ["old"])

    def test_health_alert_counts_restart_downs(self):
        z = datetime.fromtimestamp(_ts(DAY, 9, 0), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        z2 = datetime.fromtimestamp(_ts(DAY, 15, 0), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        self.write("health-alert.log", f"{z} [IMM-HEALTH] DOWN \u2014 PROCESS GONE \u2014 pid=NONE\n"
                                       f"{z2} [IMM-HEALTH] DOWN \u2014 HEARTBEAT STALE\n"
                                       f"{z2} [IMM-HEALTH] recovered\n")
        h = rv.health_alert(DAY, [_ts(DAY, 9, 2)])
        self.assertEqual(h["down_lines"], 2)
        self.assertEqual(h["within_10min_of_restart"], 1)
        self.assertEqual(h["recovered"], 1)


class MonitorTests(Base):
    def test_latest_day_at_or_before(self):
        self.write("toxic_halts_profit.json", json.dumps({"days": {
            "2026-11-23": {"halts_net": 1.5, "n_halts": 2, "junk": {"x": 1}},
            "2026-11-25": {"halts_net": 9.9}}}))
        self.write("join_wait_report.json", json.dumps({"2026-11-24": {"level": "WATCH", "net": -4.06}}))
        mons = {m["monitor"]: m for m in rv.sec_monitors(DAY)["monitors"]}
        t = mons["toxic halts"]
        self.assertEqual((t["day"], t["lag_days"]), ("2026-11-23", 1))
        self.assertEqual(t["fields"], {"halts_net": 1.5, "n_halts": 2})
        self.assertEqual(mons["join wait"]["fields"], {"level": "WATCH", "net": -4.06})
        self.assertEqual(mons["cheap-side books"]["status"], "missing")


class CounterfactualTests(Base):
    def test_classes_predictions_and_selection(self):
        rows = [{"event": "KXA-1", "series": "KXA", "pool": 900, "est": 10.0, "n": 9, "n_est": 9,
                 "reason": "capacity (cap/budget), under payout floor", "title": "a"},
                {"event": "KXB-1", "series": "KXB", "pool": 2000, "est": 0, "n": 30, "n_est": 0,
                 "reason": "not in allowlist", "title": "b"},
                {"event": "KXC-1", "series": "KXC", "pool": 500, "est": 2.0, "n": 5, "n_est": 5,
                 "reason": "blocklisted", "title": "c"}]
        with open(os.path.join(rv.CACHE_DIR, "api_gaps.pkl"), "wb") as fh:
            pickle.dump({"at": 1.0, "val": {"rows": rows, "ctx": {"programs": 10, "unest_pool": 2000}}}, fh)
        s = _ts(DAY, 3)
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        snap = [{"ts": iso(s), "ticker": "KXP-1-T1", "event_ticker": "KXP-1", "decision": "selected",
                 "est_dollars_per_day": 10.0, "program_end": iso(_ts(DAY, 20)),
                 "accrued_lifetime": 5.5, "period_base": 5.0},
                {"ts": iso(s + 3600), "ticker": "KXP-1-T1", "event_ticker": "KXP-1", "decision": "selected",
                 "est_dollars_per_day": 10.0, "program_end": iso(_ts(DAY, 20)),
                 "accrued_lifetime": 5.95, "period_base": 5.0},
                {"ts": iso(s), "ticker": "KXQ-1-T1", "event_ticker": "KXQ-1", "decision": "payout_floor",
                 "est_dollars_per_day": 4.0}]
        for u in rv.utc_dates(DAY):
            self.jsonl(f"selection_snapshot_{u}.jsonl", [r for r in snap if r["ts"][:10] == u])
        cf = rv.sec_counterfactual(DAY)
        self.assertEqual(cf["by_class"]["capacity"]["rows"], 1)
        self.assertAlmostEqual(cf["by_class"]["capacity"]["est_adj"], 7.2)
        self.assertEqual(cf["top_estimated"][0]["event"], "KXA-1")
        self.assertEqual(cf["top_unestimated_pool"][0]["event"], "KXB-1")
        sel = cf["selection"]
        self.assertEqual(sel["snapshots"], 2)
        self.assertAlmostEqual(sel["decisions"]["payout_floor"]["avg_est_day"], 2.0)
        self.assertEqual(sel["floor_misses"]["near_090_100"], 1)   # banked 0.95 at period end
        preds = list(rv._iter_jsonl(os.path.join(self.review, "counterfactual.jsonl")))
        self.assertEqual({p["event"] for p in preds}, {"KXA-1", "KXC-1"})
        rv.sec_counterfactual(DAY)                              # idempotent per day
        self.assertEqual(len(list(rv._iter_jsonl(os.path.join(self.review, "counterfactual.jsonl")))), 2)

    def test_unmeasured_pool_by_class(self):
        # R-261124-21: the gap scan's unread pool, split by who (if anyone) measures it
        rows = [{"event": "KXBLK-1", "series": "KXBLK", "pool": 100, "est": None, "n": 3, "n_est": 0,
                 "reason": "blocklisted", "title": "blk"},
                {"event": "KXNE-1", "series": "KXNE", "pool": 200, "est": None, "n": 4, "n_est": 0,
                 "reason": "not in allowlist", "title": "ne1"},
                {"event": "KXNE-2", "series": "KXNE", "pool": 50, "est": None, "n": 2, "n_est": 0,
                 "reason": "not in allowlist", "title": "ne2"},
                {"event": "KXNS-1", "series": "KXNS", "pool": 30, "est": None, "n": 1, "n_est": 0,
                 "reason": "not in allowlist", "title": "ns"},
                {"event": "KXP-2", "series": "KXP", "pool": 300, "est": None, "n": 2, "n_est": 0,
                 "reason": "screened: extreme_mid", "title": "p2"},
                {"event": "KXZ-1", "series": "KXZ", "pool": 40, "est": None, "n": 1, "n_est": 0,
                 "reason": "capacity (cap/budget)", "title": "z"},
                {"event": "KXA-1", "series": "KXA", "pool": 999, "est": 5.0, "n": 2, "n_est": 2,
                 "reason": "capacity (cap/budget)", "title": "a"}]
        with open(os.path.join(rv.CACHE_DIR, "api_gaps.pkl"), "wb") as fh:
            pickle.dump({"at": 1.0, "val": {"rows": rows, "ctx": {"unest_pool": 720}}}, fh)
        with open(os.path.join(rv.CACHE_DIR, "api_unquoted.pkl"), "wb") as fh:
            pickle.dump({"at": 1.0, "val": {"rows": [{"series": "KXNE", "scored": 4, "x1": 2.0, "x3": 5.0}],
                                            "ctx": {}}}, fh)
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        snap = [{"ts": iso(_ts(DAY, 3)), "ticker": "KXP-2-T2", "event_ticker": "KXP-2",
                 "decision": "extreme_mid", "est_dollars_per_day": 9.0},       # superseded by the later read
                {"ts": iso(_ts(DAY, 9)), "ticker": "KXP-2-T1", "event_ticker": "KXP-2",
                 "decision": "extreme_mid", "est_dollars_per_day": 0.6},
                {"ts": iso(_ts(DAY, 9)), "ticker": "KXP-2-T2", "event_ticker": "KXP-2",
                 "decision": "extreme_mid", "est_dollars_per_day": 3.0}]
        for u in rv.utc_dates(DAY):
            self.jsonl(f"selection_snapshot_{u}.jsonl", [r for r in snap if r["ts"][:10] == u])
        cf = rv.sec_counterfactual(DAY)
        um = cf["unmeasured"]
        c = um["classes"]
        self.assertEqual(um["gap_scan_unest_pool"], 720)
        self.assertEqual(c["blocked on purpose"]["pool"], 100)
        self.assertEqual((c["not enrolled: estimated"]["events"], c["not enrolled: estimated"]["pool"]), (2, 250))
        self.assertEqual(c["not enrolled: estimated"]["est_x1"], 2.0)       # per series, counted once
        self.assertEqual(c["not enrolled: estimated"]["est_x3"], 5.0)
        self.assertEqual(c["not enrolled: unscored"]["pool"], 30)
        self.assertAlmostEqual(c["allowed: estimated by the bot"]["est"], 3.6)
        self.assertAlmostEqual(c["allowed: estimated by the bot"]["est_under_1_per_mkt"], 0.6)
        self.assertEqual(c["allowed: not in the bot's snapshot"]["pool"], 40)
        self.assertEqual(um["unmeasured_pool"], 170)
        self.assertEqual(um["unmeasured_not_blocked"], 70)
        by = {x["event"]: x["measured_by"] for x in cf["top_unestimated_pool"]}
        self.assertEqual(by["KXP-2"], "allowed: estimated by the bot")
        self.assertEqual(by["KXBLK-1"], "blocked on purpose")
        md = rv.render_evidence({"day": DAY.isoformat(), "window_et": ["a", "b"], "generated": 0, "counterfactual": cf})
        self.assertIn("Unmeasured pool by class (R-261124-21)", md)
        self.assertIn("| blocked on purpose | 1 | $100 | none |", md)

    def test_floor_misses_split_forced_and_cost(self):
        # R-261124-23: misses split by whether the bot quoted them to the end, the
        # wave that forced them, what they cost and the size they needed
        pe = _ts(DAY, 20)
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        base = {"program_end": iso(pe), "period_base": 1.0, "est_dollars_per_day": 1.0}
        snap = [dict(base, ts=iso(_ts(DAY, 18)), ticker="KXW-1-T1", event_ticker="KXW-1",
                     decision="selected", accrued_lifetime=1.4),
                dict(base, ts=iso(_ts(DAY, 18)), ticker="KXW-1-T2", event_ticker="KXW-1",
                     decision="selected", accrued_lifetime=1.1),
                dict(base, ts=iso(_ts(DAY, 18)), ticker="KXD-1-T1", event_ticker="KXD-1",
                     decision="hopeless", accrued_lifetime=1.3)]
        for u in rv.utc_dates(DAY):
            self.jsonl(f"selection_snapshot_{u}.jsonl", [r for r in snap if r["ts"][:10] == u])
        self.write("post_vote_dead_books.json", json.dumps({"markets": {
            "KXW-1-T1": {"event": "KXW-1", "stop": iso(pe + 7 * 86400),
                         "band": "2026-11-24T05:00:00Z: 1-99c (wave w1111-test)"},
            "KXOLD-1": {"event": "KXD-1", "stop": iso(pe - 10 * 86400)}}}))
        self.archive(DAY, {"KXW-1-T1": _row(pnl=-5.0, fills=2, rest=100.0),
                           "KXW-1-T2": _row(pnl=-1.0, fills=1, rest=50.0),
                           "KXD-1-T1": _row(pnl=1.5, fills=1, rest=10.0)})
        self.archive(DAY - timedelta(days=8), {"KXW-1-T1": _row(pnl=-100.0, fills=9, rest=1.0)})
        fm = rv.selection_day(DAY)["floor_misses"]
        self.assertEqual(fm["under_090"], 3)
        self.assertEqual(fm["split"]["quoted_to_end"]["n"], 2)
        self.assertEqual(fm["split"]["dropped"]["n"], 1)
        self.assertEqual(fm["wave_forced"]["n"], 2)                  # the wave forces its whole event
        self.assertEqual(fm["wave_forced"]["by_wave"], [("w1111-test", 2)])
        self.assertEqual(fm["size_needed"], {"<=2x": 0, "2-3x": 1, "3-5x": 0, ">5x": 1})
        q = fm["cost_7d"]["quoted_to_end"]
        self.assertEqual((q["pnl"], q["fills"], q["rest_avg"]), (-6.0, 3, 75.0))   # day -8 is outside
        self.assertEqual(fm["cost_7d"]["dropped"]["pnl"], 1.5)
        md = rv.render_evidence({"day": DAY.isoformat(), "window_et": ["a", "b"], "generated": 0, "counterfactual": {"selection": {
            "decisions": {"selected": {"tickers": 2, "avg_est_day": 1.0}}, "floor_misses": fm}}})
        self.assertIn("2 QUOTED TO THE END", md)
        self.assertIn("1 DROPPED by the floor logic", md)

    def test_floor_miss_cost_without_wave_lists_or_archive(self):
        pe = _ts(DAY, 20)
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        self.jsonl(f"selection_snapshot_{rv.utc_dates(DAY)[0]}.jsonl",
                   [{"ts": iso(_ts(DAY, 18)), "ticker": "KXQ-1-T1", "event_ticker": "KXQ-1",
                     "decision": "selected", "program_end": iso(pe), "accrued_lifetime": 0.5,
                     "period_base": 0.0}])
        fm = rv.selection_day(DAY)["floor_misses"]
        self.assertEqual(fm["wave_forced"]["n"], 0)
        self.assertEqual(fm["cost_7d"]["quoted_to_end"], {"pnl": 0.0, "fills": 0, "rest_avg": 0.0})
        self.assertEqual(fm["size_needed"]["<=2x"], 1)

    def test_whole_day_from_every_hourly_scan(self):
        # R-261124-24: the review adds up the day's archived gap scans, each scan
        # standing for the time to the next (<= 2h; the first also since midnight)
        s, _e = rv.et_bounds(DAY)
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        A = {"event": "KXA-1", "series": "KXA", "n": 2, "n_est": 2, "pool": 100.0, "est": 24.0,
             "reason": "screened: extreme_mid"}
        B = {"event": "KXB-1", "series": "KXB", "n": 1, "n_est": 1, "pool": 200.0, "est": 48.0,
             "reason": "not in allowlist"}
        C = {"event": "KXC-1", "series": "KXC", "n": 3, "n_est": 0, "pool": 50.0, "est": None,
             "reason": "blocklisted"}
        by_u = defaultdict(list)
        for ts, scan in ((s + 3600, [A, B, C]), (s + 3900, [A, C]), (s + 13 * 3600, [A, C])):
            by_u[datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")].append(
                {"ts": iso(ts), "source": "scan", "ctx": {}, "rows": scan})
        for u, recs in by_u.items():
            self.jsonl(f"gap_scans_{u}.jsonl", recs)
        with open(os.path.join(rv.CACHE_DIR, "api_gaps.pkl"), "wb") as fh:      # the morning scan: A, C
            pickle.dump({"at": 1.0, "val": {"rows": [dict(A), dict(C)], "ctx": {"unest_pool": 50}}}, fh)
        cf = rv.sec_counterfactual(DAY)
        wd = cf["whole_day"]
        cover = 3900 + 7200 + 7200
        self.assertEqual(wd["scans"], 3)
        self.assertEqual(wd["covered_hours"], round(cover / 3600, 1))
        top = {x["event"]: x for x in wd["top"]}
        self.assertAlmostEqual(top["KXA-1"]["est_days"], round(24.0 * cover / 86400, 2))
        self.assertAlmostEqual(top["KXB-1"]["est_days"], round(48.0 * 3900 / 86400, 2))
        self.assertFalse(top["KXB-1"]["in_morning_scan"])
        self.assertTrue(top["KXA-1"]["in_morning_scan"])
        self.assertEqual(wd["ended_before_review"]["events"], 1)
        self.assertEqual(wd["ended_before_review"]["top"][0]["event"], "KXB-1")
        self.assertAlmostEqual(wd["unread_pool_days"], round(50.0 * cover / 86400, 2))
        md = rv.render_evidence({"day": DAY.isoformat(), "window_et": ["a", "b"], "generated": 0,
                                 "counterfactual": cf})
        self.assertIn("THE WHOLE ET DAY", md)
        self.assertIn("KXB-1 [not enrolled: not in allowlist]", md)
        self.assertIn("ENDED BEFORE THE REVIEW", md)

    def test_whole_day_without_an_archive_says_so(self):
        wd = rv.sec_counterfactual(DAY)["whole_day"]
        self.assertEqual(wd["scans"], 0)
        self.assertIn("no gap_scans file", wd["note"])

    def test_whole_day_reads_compressed_gap_scans(self):
        # gap_scans joined imm_log_retention's ALLOW: a re-run for a day past 35 days
        # reads the .gz copies and adds up the same day
        import imm_log_retention as ret
        self.assertEqual(ret.ALLOW.get("gap_scans"), "jsonl")
        s, _e = rv.et_bounds(DAY)
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        A = {"event": "KXA-1", "series": "KXA", "n": 2, "n_est": 2, "pool": 100.0, "est": 24.0,
             "reason": "screened: extreme_mid"}
        by_u = defaultdict(list)
        for ts in (s + 3600, s + 13 * 3600, s + 22 * 3600):          # 22:00 ET is the next UTC day
            by_u[datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")].append(
                {"ts": iso(ts), "source": "scan", "ctx": {}, "rows": [A]})
        self.assertEqual(len(by_u), 2)
        for u, recs in by_u.items():
            self.jsonl(f"gap_scans_{u}.jsonl", recs)
        scans, wd = rv.gap_scans_of_day(DAY), rv.sec_counterfactual(DAY)["whole_day"]
        self.assertEqual(wd["scans"], 3)
        for name in os.listdir(self.status):
            if name.startswith("gap_scans_"):
                self.assertEqual(ret.compress_file(os.path.join(self.status, name))["action"], "compressed")
        self.assertEqual(sorted(n for n in os.listdir(self.status) if n.startswith("gap_scans_")),
                         sorted(f"gap_scans_{u}.jsonl.gz" for u in by_u))
        self.assertEqual(rv.gap_scans_of_day(DAY), scans)
        self.assertEqual(rv.sec_counterfactual(DAY)["whole_day"], wd)

    def test_reason_class(self):
        self.assertEqual(rv.reason_class("screened: extreme_mid, under payout floor"), "market screen")
        self.assertEqual(rv.reason_class("under payout floor, screened: x"), "economics")
        self.assertEqual(rv.reason_class("yielded to manual, not in allowlist"), "deliberate")

    def test_calibration_scores_later_quotes(self):
        old = DAY - timedelta(days=5)
        self.jsonl("daily_review/counterfactual.jsonl",
                   [{"day": old.isoformat(), "event": f"KXE{i}-1", "est": 10.0} for i in range(16)])
        for k in range(1, 3):
            self.archive(old + timedelta(days=k),
                         {f"KXE{i}-1-T1": _row(rew=5.0) for i in range(16)})
        out = rv.calibrate_predictions(DAY)
        self.assertEqual(out["n"], 16)
        self.assertAlmostEqual(out["factor"], 0.5)
        self.assertEqual(rv.realisation_factor()[0], 0.5)


class CoverageDataConfigTests(Base):
    def test_cycle_gaps(self):
        s = _ts(DAY, 0, 0)
        times = [s + 30 * i for i in range(10)] + [s + 30 * 9 + 1800]
        for u in rv.utc_dates(DAY):
            lines = ["ts,ticker,x"]
            for t in times:
                z = datetime.fromtimestamp(t, timezone.utc)
                if z.date().isoformat() == u:
                    stamp = z.strftime("%Y-%m-%dT%H:%M:%SZ")
                    lines += [f"{stamp},KXA-1,1", f"{stamp},KXB-1,1"]
            self.write(f"cycle_log_{u}.csv", "\n".join(lines) + "\n")
        cv = rv.sec_coverage(DAY)
        self.assertEqual(cv["cycles"], 11)
        self.assertEqual(cv["gap_max_s"], 1800.0)
        self.assertAlmostEqual(cv["dark_minutes"], 20.0)

    def test_sink_status(self):
        for i in range(1, 15):
            d = (DAY - timedelta(days=i)).isoformat()
            self.write(f"orders_{d}.jsonl", "x" * 1000)
            self.write(f"marks_{d}.jsonl", "x" * 1000)
        self.write(f"orders_{DAY.isoformat()}.jsonl", "x" * 100)
        self.write(f"imm_toxic_halts_sent_{DAY.isoformat()}.marker", "x")
        with mock.patch.object(rv, "SINK_NOISE_FLOOR_BYTES", 0):
            dh = rv.sec_data(DAY)
        st = {r["sink"]: r["status"] for r in dh["sinks"]}
        self.assertEqual(st["orders"], "LOW")
        self.assertEqual(st["marks"], "MISSING")
        self.assertNotIn("imm_toxic_halts_sent", st)
        # a sink whose usual day is under the noise floor is event-driven: not LOW
        st2 = {r["sink"]: r["status"] for r in rv.sec_data(DAY)["sinks"]}
        self.assertEqual(st2["orders"], "ok")
        self.assertEqual(st2["marks"], "MISSING")          # missing is still missing

    def test_config_versioning(self):
        self.write("extra_allow_series.json", json.dumps({"a": 1}))
        self.write("UNWIND_ONLY_WAVES", "")
        self.write("status_incentive_mm.json", "{}")
        first = rv.snapshot_config(DAY - timedelta(days=1))
        self.assertEqual(first["tracked_files"], 2)
        self.write("extra_allow_series.json", json.dumps({"a": 1, "b": 2}))
        out = rv.snapshot_config(DAY)
        self.assertEqual(out["changes"][0]["file"], "extra_allow_series.json")
        self.assertIn("+1 keys (b)", out["changes"][0]["change"])


class LedgerTests(Base):
    def findings(self, day, items, **extra):
        p = os.path.join(self.review, day.isoformat(), "findings.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            json.dump({"findings": items, **extra}, fh)
        return p

    def test_lifecycle(self):
        f = {"fingerprint": "ops:sync-wedged", "kind": "ops", "severity": "p1", "title": "Sync wedged",
             "decision_needed": True, "metric": {"eval_after_days": 3, "recipe": "x"},
             "impact": {"usd_day": 0, "basis": "measured"}}
        self.findings(DAY, [f, {"title": "No fingerprint", "kind": "weird"}], lessons=["learned x"])
        res = rv.ledger_ingest(DAY)
        iid = res["ids"]["ops-sync-wedged"]
        self.assertEqual(iid, "R-261124-01")
        self.assertEqual(len(res["created"]), 2)
        it = rv.ledger_state()[iid]
        self.assertEqual(it["status"], "proposed")
        self.assertEqual(it["severity"], "P1")
        self.assertEqual(rv.ledger_state()["R-261124-02"]["kind"], "review")
        # same day again: no duplicate rows
        rv.ledger_ingest(DAY)
        self.assertEqual(len(rv.ledger_state()), 2)
        # next day: matched by fingerprint, seen twice
        d2 = DAY + timedelta(days=1)
        self.findings(d2, [f])
        res2 = rv.ledger_ingest(d2)
        self.assertEqual(res2["seen"], [iid])
        self.assertEqual(rv.ledger_state()[iid]["seen_days"], [DAY.isoformat(), d2.isoformat()])
        # decide, ship (by commit id), due, eval
        rv.ledger_append({"op": "decide", "id": iid, "status": "approved", "by": "jack", "note": "yes"})
        ev = rv.sec_ledger(d2, [{"sha": "abc1234", "subject": f"imm: fix ({iid})", "ids": [iid]}])
        self.assertEqual(ev["shipped_detected"], [iid])
        it = rv.ledger_state()[iid]
        self.assertEqual(it["status"], "shipped")
        ship_day = datetime.fromtimestamp(rv._ts_of(it["ship"]["at"]), rv.ET).date()
        self.assertEqual(rv.ledger_due(ship_day + timedelta(days=2)), [])
        self.assertEqual(rv.ledger_due(ship_day + timedelta(days=3))[0]["id"], iid)
        d3 = ship_day + timedelta(days=3)
        self.findings(d3, [], evaluations=[{"id": iid, "verdict": "verified", "delta_usd_day": 12.5}],
                      closures=[{"id": "R-261124-02", "status": "wontfix", "note": "by design"}])
        rv.ledger_ingest(d3)
        st = rv.ledger_state()
        self.assertEqual(st[iid]["status"], "verified")
        self.assertEqual(st["R-261124-02"]["status"], "wontfix")
        self.assertEqual(rv.ledger_stats()["verified_delta_usd_day"], 12.5)
        self.assertEqual(rv.ledger_due(d3), [])
        with open(os.path.join(self.review, "lessons.md")) as fh:
            self.assertIn("learned x", fh.read())

    def test_closed_fingerprint_reopens_as_new_item(self):
        f = {"fingerprint": "bug:x", "kind": "bug", "title": "x"}
        self.findings(DAY, [f])
        a = rv.ledger_ingest(DAY)["ids"]["bug-x"]
        rv.ledger_append({"op": "close", "id": a, "status": "closed"})
        d2 = DAY + timedelta(days=1)
        self.findings(d2, [f])
        b = rv.ledger_ingest(d2)["ids"]["bug-x"]
        self.assertNotEqual(a, b)

    def test_put_writes_only_whitelisted_files(self):
        p = rv.put(DAY, "findings.json", '{"findings": []}')
        self.assertTrue(p.endswith(os.path.join(DAY.isoformat(), "findings.json")))
        self.assertEqual(rv._load_json(p), {"findings": []})
        with self.assertRaises(SystemExit):
            rv.put(DAY, "findings.json", "[1, 2]")
        with self.assertRaises(SystemExit):
            rv.put(DAY, "findings.json", "{not json")
        with self.assertRaises(SystemExit):
            rv.put(DAY, "../../incentive_mm.py", "x")
        p2 = rv.put(DAY, "review.md", "# hi\n\n\n")
        with open(p2, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# hi\n")

    def test_cli_decide_and_show(self):
        self.findings(DAY, [{"fingerprint": "data:y", "kind": "data", "title": "y"}])
        rv.ledger_ingest(DAY)
        self.assertEqual(rv.main(["ledger", "decide", "R-261124-01", "rejected", "--note", "no"]), 0)
        self.assertEqual(rv.ledger_state()["R-261124-01"]["status"], "rejected")
        self.assertEqual(rv.main(["ledger", "show", "R-999999-01"]), 1)
        self.findings(DAY, [{"fingerprint": "data:z", "kind": "data", "title": "z"}])
        rv.ledger_ingest(DAY)
        iid = [k for k, v in rv.ledger_state().items() if v["fingerprint"] == "data-z"][0]
        self.assertEqual(rv.main(["ledger", "close", iid, "--status", "fixed-elsewhere", "--note", "x"]), 0)
        self.assertEqual(rv.ledger_state()[iid]["status"], "fixed-elsewhere")


class RenderTests(unittest.TestCase):
    def test_tables_lists_money_and_ids(self):
        md = "\n".join([
            "Lead line with **bold** and `code`.", "",
            "| Market | Trading |", "|---|---:|", "| KXA | -$88 |", "| KXB | +$46 |", "",
            "- **One:** first", "  - nested", "- **Two:** see R-261124-02", "",
            "1. step one", "2. step two", ""])
        h = sr.md_html(md)
        self.assertIn("<b>bold</b>", h)
        self.assertIn("<code", h)
        self.assertIn("<table", h)
        self.assertIn("text-align:right", h)                          # ---: aligns right
        self.assertIn(f"color:{sr.RED}", h)                           # -$88 in red
        self.assertIn(f"color:{sr.GREEN}", h)                         # +$46 in green
        self.assertEqual(h.count("<ul"), 2)                           # nested list
        self.assertIn("<ol", h)
        self.assertIn("R-261124-02</span>", h)                        # id tag
        self.assertNotIn("**", h)

    def test_table_inside_a_numbered_item_keeps_the_list_together(self):
        md = "\n".join(["1. **First**", "   - a", "", "     | x | y |", "     |---|---:|", "     | 1 | 2 |", "",
                        "   - b", "", "2. **Second** with **`code` inside bold**", ""])
        h = sr.md_html(md)
        self.assertEqual(h.count("<ol"), 1)                               # one list: numbers continue
        self.assertLess(h.index("<table"), h.index("Second"))            # the table stays in item 1
        self.assertLess(h.index('">b</li>'), h.index("Second"))          # so does the bullet after it
        self.assertIn("<b><code", h)                                     # bold wraps code
        self.assertNotIn("**", h)
        self.assertIn('start="3"', sr.md_html("3. third\n4. fourth\n"))  # a list starting at 3

    def test_escapes_html(self):
        self.assertIn("&lt;script&gt;", sr.md_html("a <script>x</script> b"))

    def test_split_review(self):
        title, intro, secs = sr.split_review("\n".join([
            "# Tue: verdict", "Intro line.", "", "## What happened", "- a", "## Data", "b", ""]))
        self.assertEqual(title, "Tue: verdict")
        self.assertEqual(intro, "Intro line.")
        self.assertEqual([h for h, _ in secs], ["What happened", "Data"])


class MoatTests(Base):
    def _cycle_log(self, day, rows):
        """rows: (hh, mm, ticker, est_frac, pool) -> cycle_log files by UTC date."""
        by_u = defaultdict(list)
        for hh, mm, t, frac, pool in rows:
            z = datetime.fromtimestamp(_ts(day, hh, mm), timezone.utc)
            by_u[z.date().isoformat()].append(f"{z.strftime('%Y-%m-%dT%H:%M:%SZ')},{t},1,2,{frac},{pool}")
        for u, lines in by_u.items():
            self.write(f"cycle_log_{u}.csv", "ts,ticker,ext_bid,ext_ask,est_frac,pool_per_day\n"
                       + "\n".join(lines) + "\n")

    def test_cycle_share_is_pool_weighted(self):
        self._cycle_log(DAY, [(9, 0, "KXAAA-1-T1", 0.10, 100), (9, 0, "KXAAA-2-T1", 0.30, 300),
                              (9, 1, "KXAAA-1-T1", 0.10, 100), (9, 1, "KXAAA-2-T1", 0.30, 300),
                              (9, 1, "KXBBB-1-T1", 0.50, 0)])            # no pool: ignored
        sh = rv.cycle_share(DAY)
        self.assertAlmostEqual(sh["Fam KXAA"]["share"], (0.1 * 100 + 0.3 * 300) / 400)
        self.assertAlmostEqual(sh["Fam KXAA"]["pool_cov"], 400.0)       # per cycle, 2 cycles
        self.assertNotIn("Fam KXBB", sh)

    def test_a_rerun_for_an_old_day_reads_compressed_logs(self):
        # R-261124-03: day files older than 35 days are .gz; a re-run for one reads the same
        import imm_log_retention as ret
        self._cycle_log(DAY, [(9, 0, "KXAAA-1-T1", 0.10, 100), (9, 1, "KXAAA-1-T1", 0.10, 100),
                              (21, 0, "KXAAA-1-T1", 0.20, 100)])           # 21:00 ET is the next UTC day
        snap = self.jsonl(f"selection_snapshot_{DAY.isoformat()}.jsonl", [{"ts": "x", "ticker": "KXAAA-1-T1"}])
        share, times = rv.cycle_share(DAY), rv.cycle_times(DAY)
        rows = list(rv._iter_jsonl(snap))
        for name in os.listdir(self.status):
            if name.startswith(("cycle_log_", "selection_snapshot_")):
                self.assertEqual(ret.compress_file(os.path.join(self.status, name))["action"], "compressed")
        self.assertFalse(os.path.exists(snap))
        self.assertEqual((rv.cycle_share(DAY), rv.cycle_times(DAY)), (share, times))
        self.assertEqual(list(rv._iter_jsonl(snap)), rows)
        self.assertEqual(len(times), 3)

    def test_history_priors_and_edge_watch(self):
        # 28 days: KXGOOD steady; KXFADE's reward per $ resting halves in the last 7
        for i in range(28):
            d = DAY - timedelta(days=27 - i)
            fade = 5.0 if i >= 21 else 10.0
            self.archive(d, {"KXGOOD-1-T1": _row(rew=10, pnl=-2, rest=1000),
                             "KXFADE-1-T1": _row(rew=fade, pnl=-1, rest=1000)})
        self._cycle_log(DAY, [(9, 0, "KXGOOD-1-T1", 0.2, 100)])
        mo = rv.sec_moat(DAY)
        rows = rv._read_rows(os.path.join(rv.MOAT_DIR, "family_daily.csv"))
        self.assertEqual(len(rows), 56)
        self.assertEqual(mo["assets"]["days"], 28)
        today = {r["family"]: r for r in rows if r["day"] == DAY.isoformat()}
        self.assertAlmostEqual(float(today["Fam KXGO"]["share"]), 0.2)
        edge = {e["family"]: e for e in mo["edge"]}
        self.assertEqual(edge["Fam KXFA"]["verdict"], "eroding")
        self.assertAlmostEqual(edge["Fam KXFA"]["rew_per_1k_ratio"], 0.5)
        self.assertNotIn("Fam KXGO", edge)                                # steady
        self.assertTrue(all(e["share_ratio"] is None for e in mo["edge"]))  # 1 share day: not used
        pri = rv._load_json(os.path.join(rv.MOAT_DIR, "priors.json"))
        g = pri["families"]["Fam KXGO"]
        self.assertAlmostEqual(g["raw"]["rew_per_1k"], 10.0)
        self.assertAlmostEqual(g["raw"]["tax"], 0.2)
        # second run: nothing re-added except the day itself; share kept
        mo2 = rv.sec_moat(DAY)
        self.assertEqual(mo2["rows_added"], 2)
        self.assertEqual(len(rv._read_rows(os.path.join(rv.MOAT_DIR, "family_daily.csv"))), 56)

    def test_counterfactual_nets_by_family_tax(self):
        os.makedirs(rv.MOAT_DIR, exist_ok=True)
        rv._write_json(os.path.join(rv.MOAT_DIR, "priors.json"), {
            "book": {"tax": 0.5}, "families": {"Fam KXTA": {"prior": {"tax": 0.8}}}})
        rows = [{"event": "KXTAX-1", "series": "KXTAX", "pool": 100, "est": 10.0, "n": 1, "n_est": 1,
                 "reason": "capacity"},
                {"event": "KXNEW-1", "series": "KXNEW", "pool": 100, "est": 8.0, "n": 1, "n_est": 1,
                 "reason": "capacity"}]
        with open(os.path.join(rv.CACHE_DIR, "api_gaps.pkl"), "wb") as fh:
            pickle.dump({"at": 1.0, "val": {"rows": rows, "ctx": {}}}, fh)
        top = {x["event"]: x for x in rv.sec_counterfactual(DAY)["top_estimated"]}
        self.assertAlmostEqual(top["KXTAX-1"]["est_net"], round(10 * 0.72 * 0.2, 2))
        self.assertAlmostEqual(top["KXNEW-1"]["est_net"], round(8 * 0.72 * 0.5, 2))  # book tax
        self.assertEqual([x["event"] for x in rv.sec_counterfactual(DAY)["top_estimated"]],
                         ["KXNEW-1", "KXTAX-1"])                          # ranked by net

    def test_playbook_rules_are_appended_by_ingest(self):
        p = os.path.join(self.review, DAY.isoformat(), "findings.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            json.dump({"findings": [], "playbook": [
                {"rule": "new ladders start at x1", "applies_to": "event ladders", "evidence": "R-261124-02"},
                {"rule": "  "}]}, fh)
        rv.ledger_ingest(DAY)
        with open(os.path.join(rv.MOAT_DIR, "playbook.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("[R-261124-02] event ladders: new ladders start at x1", text)
        self.assertEqual(text.count("\n- "), 1)

    def test_edge_line_in_the_email(self):
        ev = {"moat": {"edge": [{"family": "Elections", "verdict": "eroding", "lead": 0.65,
                                 "share_ratio": 0.65},
                                {"family": "Rotten Tomatoes", "verdict": "strengthening", "lead": 1.4,
                                 "share_ratio": None}]}}
        self.assertEqual(sr.edge_line(ev), "Elections pool share -35% (eroding); "
                                           "Rotten Tomatoes rewards per $1k resting +40% (strengthening)")


class RetentionStatusTests(Base):
    """R-261124-03: the evidence pack reports imm_log_retention's last run."""

    def test_reads_last_run_and_the_manifest(self):
        import time
        self.assertEqual(rv.retention_status(), {"present": False})
        rdir = os.path.join(self.status, "retention")
        os.makedirs(rdir)
        now = time.time()
        with open(os.path.join(rdir, "last_run.json"), "w") as fh:
            json.dump({"ts": datetime.fromtimestamp(now - 3600, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "mode": "apply", "compressed": 2, "in_gb": 1.5, "freed_gb": 1.2, "remaining": 3,
                       "remaining_gb": 4.0, "held": 1, "held_reasons": {"ET day not in the day archive": 1},
                       "failed": [{"file": "orders_2026-10-01.jsonl", "reason": "verify failed"}],
                       "conflicts": []}, fh)
        with open(os.path.join(rdir, "manifest.jsonl"), "w") as fh:
            for r in ({"action": "compressed", "size": 1000, "gz_size": 100},
                      {"action": "removed_plain", "size": 500},
                      {"action": "failed", "size": 7},
                      {"action": "restored", "size": 1000}):
                fh.write(json.dumps(r) + "\n")
        rt = rv.retention_status(now=now)
        self.assertEqual((rt["mode"], rt["compressed"], rt["age_h"]), ("apply", 2, 1.0))
        self.assertEqual((rt["lifetime_files"], rt["lifetime_restored"]), (2, 1))
        md = rv.render_evidence({"day": DAY.isoformat(), "window_et": ["a", "b"], "generated": now,
                                 "system": {"retention": rt}})
        self.assertIn("Log retention (imm_log_retention, R-261124-03)", md)
        self.assertIn("2 files compressed (1.5 GB in), 1.2 GB freed", md)
        self.assertIn("Log retention FAILURES", md)
        self.assertNotIn("STALE", md)
        md0 = rv.render_evidence({"day": DAY.isoformat(), "window_et": ["a", "b"], "generated": now,
                                  "system": {"retention": {"present": False}}})
        self.assertIn("Log retention: no run yet", md0)


class MeasureTests(Base):
    def test_before_after(self):
        split = DAY
        for i in range(1, 4):
            self.archive(split - timedelta(days=i), {"KXRT-A-1": _row(rew=10, pnl=-5), "KXZZ-B-1": _row(rew=99)})
            self.archive(split + timedelta(days=i), {"KXRT-A-1": _row(rew=12, pnl=1), "KXZZ-B-1": _row(rew=99)})
        out = rv.measure("^KXRT", None, split.isoformat(), 3, 3)
        self.assertEqual(out["before"]["days"], 3)
        self.assertAlmostEqual(out["before"]["net_day"], 5)
        self.assertAlmostEqual(out["after"]["net_day"], 13)
        self.assertAlmostEqual(out["delta_net_day"], 8)


class CollectAndSendTests(Base):
    def setUp(self):
        super().setUp()
        self.archive(DAY, {"KXAAA-1-T1": _row(rew=50, pnl=-10)})
        self.write("status_incentive_mm.json", json.dumps({"updated_at": "2026-11-25T09:00:00Z",
                                                           "markets_line": "1/2 mkts quoted"}))
        os.makedirs(os.path.join(self.root, "run-logs"), exist_ok=True)
        with open(os.path.join(self.root, "run-logs", "sync-kl-main.log"), "w") as fh:
            fh.write("2026-11-24 00:15:02 Already up to date.\n2026-11-24 00:45:02 ! MERGE FAILED (exit 128)\n")

    def _collect(self):
        with mock.patch.object(rv, "_ps_json", return_value=[{"n": "KL imm x", "s": "Ready", "r": 1, "l": "t"}]), \
                mock.patch.object(rv, "_run", return_value=(0, "")):
            return rv.collect(DAY, bootstrap_days=0)

    def test_collect_writes_pack_and_survives_missing_inputs(self):
        ev = self._collect()
        d = os.path.join(self.review, DAY.isoformat())
        self.assertTrue(os.path.exists(os.path.join(d, "evidence.json")))
        with open(os.path.join(d, "evidence.md"), encoding="utf-8") as fh:
            md = fh.read()
        self.assertIn("## 1. Scorecard", md)
        self.assertIn("## 9. Ledger", md)
        md.encode("cp1252", errors="strict")
        self.assertAlmostEqual(ev["scorecard"]["day_totals"]["net"], 40)
        self.assertEqual(ev["system"]["sync"]["failure_streak"], 1)
        self.assertEqual(ev["system"]["tasks"]["failing"][0]["task"], "KL imm x")
        self.assertIn("counterfactual", ev)
        with mock.patch.object(rv, "sec_attribution", side_effect=RuntimeError("boom")), \
                mock.patch.object(rv, "_ps_json", return_value=[]), \
                mock.patch.object(rv, "_run", return_value=(0, "")):
            ev2 = rv.collect(DAY, bootstrap_days=0)
        self.assertIn("boom", ev2["errors"]["attribution"])

    def _write_review(self):
        d = os.path.join(self.review, DAY.isoformat())
        with open(os.path.join(d, "review.md"), "w", encoding="utf-8") as fh:
            fh.write("# Verdict\n\n1. Fix the sync [[fp:ops:sync-wedged]]\n")
        with open(os.path.join(d, "findings.json"), "w") as fh:
            json.dump({"verdict": "A clean day; one thing needs you.", "findings": [
                {"fingerprint": "ops:sync-wedged", "kind": "ops", "severity": "P1", "title": "t",
                 "decision_needed": True,
                 "email": {"headline": "The repo sync is stuck", "why": "GitHub and the box diverged.",
                           "fix": "Merge and push.", "value": "keeps code backed up"}},
                {"fingerprint": "bug:long", "kind": "bug", "severity": "P2",
                 "title": "A very long technical title " + "x" * 120,
                 "summary": "First sentence explains it. Second sentence does not show.",
                 "impact": {"usd_day": 12.4, "basis": "measured"}}]}, fh)
        rv.ledger_ingest(DAY)
        return d

    def test_email_fields_fall_back_to_short_versions(self):
        f = {"title": "word " * 40, "summary": "It breaks. Then more.", "plan": {"change": "Do X. And Y."},
             "impact": {"usd_day": 25, "basis": "modelled"}}
        e = sr.email_fields(f)
        self.assertLessEqual(len(e["headline"]), 93)
        self.assertTrue(e["headline"].endswith("..."))
        self.assertEqual(e["why"], "It breaks.")
        self.assertEqual(e["fix"], "Do X.")
        self.assertEqual(e["value"], "~$25/day (modelled)")

    def test_email_cards_numbers_and_attachment(self):
        self._collect()
        d = self._write_review()
        subject, page, text, attach = sr.build(DAY)
        self.assertEqual(subject, "IMM review Tue 11/24: +$40 net | 1 needs your OK | The repo sync is stuck")
        self.assertIn("Needs your OK", page)
        self.assertIn("The repo sync is stuck", page)
        self.assertIn("do R-261124-01", page)
        self.assertIn("A very long technical title", page)          # in Everything found today
        self.assertIn("Everything found today", page)
        self.assertIn("A clean day; one thing needs you.", page)
        self.assertIn("Fix the sync", page)                          # the review.md body is in the email
        self.assertIn("Health checks", page)
        self.assertNotIn("[[fp:", page)
        self.assertLess(len(page), 100_000)                          # under Gmail's clip size
        self.assertIn('Approve: text "do R-261124-01"', text)
        self.assertIn("Fix the sync R-261124-01", text)               # review.md, ids substituted
        name, ev_page = attach[0]
        self.assertEqual(name, "IMM-evidence-2026-11-24.html")
        self.assertIn("Scorecard", ev_page)
        sent = []

        class FakeSMTP:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, *a):
                pass

            def sendmail(self, frm, rcpt, msg):
                sent.append(rcpt)

        env = {"ALERT_EMAIL_FROM": "bot@example.test", "ALERT_EMAIL_PASSWORD": "pw",
               "ALERT_EMAIL_TO": "owner@example.test"}
        with mock.patch.dict(os.environ, env), mock.patch.object(sr.smtplib, "SMTP_SSL", FakeSMTP):
            self.assertEqual(sr.main(["--day", DAY.isoformat()]), 0)
            self.assertEqual(sr.main(["--day", DAY.isoformat()]), 0)      # marker: no second send
        self.assertEqual(sent, [["owner@example.test"]])
        self.assertTrue(os.path.exists(os.path.join(d, "sent.marker")))

    def test_fallback_when_no_review(self):
        self._collect()
        subject, page, text, attach = sr.build(DAY, fallback=True, reason="claude exit 1")
        self.assertEqual(subject, "IMM review Tue 11/24: +$40 net | numbers only, analysis did not run")
        self.assertIn("The analysis did not run", page)
        self.assertIn("claude exit 1", page)
        self.assertNotIn("Needs your OK", page)
        self.assertIn("The analysis did not run (claude exit 1)", text)
        self.assertEqual(attach[0][0], "IMM-evidence-2026-11-24.html")
        self.assertIn("Scorecard", attach[0][1])
        self.assertIn("1. Scorecard", page)                          # the evidence pack in the body
        _, p2, _, _ = sr.build(DAY)                                         # no review.md at all
        self.assertIn("no review was written", p2)

    def test_dry_run_writes_html_only(self):
        self._collect()
        with mock.patch.object(sr, "send") as snd:
            self.assertEqual(sr.main(["--day", DAY.isoformat(), "--dry", "--fallback"]), 0)
        snd.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(self.review, DAY.isoformat(), "email.html")))


if __name__ == "__main__":
    unittest.main()
