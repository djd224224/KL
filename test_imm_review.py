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

    def test_only_the_subject_ships_an_item(self):
        # d4d28c2 (11/26): the body opens "On the daily review's R-261125-01 ..." and
        # shipped something else; reading the body would have marked R-261125-01 shipped
        out = ("\x1ed4d28c2\x1f1795600000\x1fimm: the RCP weekly stands down mornings (Jack: \"Yes, build it\")\x1f"
               "On the daily review's R-261125-01 and Jack's question\n\x1d\n\nincentive_mm.py\n"
               "\x1e205a966\x1f1795600100\x1fimm: state save retry (review R-261124-05, -06 and -07; Jack)\x1f\x1d\n\nx.py\n")
        rows = rv.parse_git_log(out)
        self.assertEqual((rows[0]["ids"], rows[0]["mentions"]), ([], ["R-261125-01"]))
        self.assertEqual(rows[1]["ids"], ["R-261124-05", "R-261124-06", "R-261124-07"])
        rv.ledger_append({"op": "create", "id": "R-261125-01", "day": "2026-11-25", "first_seen": "2026-11-25",
                          "status": "proposed", "kind": "strategy", "fingerprint": "s", "title": "x5 size"})
        led = rv.sec_ledger(DAY, rows)
        self.assertEqual(led["shipped_detected"], [])
        self.assertEqual(rv.ledger_state()["R-261125-01"]["status"], "proposed")

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

    def test_idle_days_do_not_crash(self):
        """The 11/26 headless run: measure died on a scope idle for a day."""
        split = DAY
        for i in range(1, 4):
            self.archive(split - timedelta(days=i), {"KXZZ-B-1": _row(rew=99)})          # the scope idle before
            self.archive(split + timedelta(days=i), {"KXRT-A-1": _row(rew=12, fills=2, rest=500)})
        out = rv.measure("^KXRT", None, split.isoformat(), 3, 3)
        self.assertEqual((out["before"]["net_day"], out["after"]["net_day"]), (0.0, 12.0))


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


class CompoundTests(Base):
    """The daily self-question (Jack 2026-11-26: "what else should this skill be
    doing daily to compound and improve itself? everyday it should ask itself
    this question and come with opportunities")."""

    def _findings(self, extra=None):
        d = os.path.join(self.review, DAY.isoformat())
        os.makedirs(d, exist_ok=True)
        data = {"findings": [{"fingerprint": "ops:x", "kind": "ops", "severity": "P2", "title": "x",
                              "decision_needed": True}],
                "compound": [
                    {"fingerprint": "review:trace-tool", "title": "a market timeline tool",
                     "gap": "fills+marks grepped by hand in 3 runs", "change": "imm_review.py trace",
                     "email": {"headline": "Give the review a market timeline", "fix": "one command"}},
                    {"fingerprint": "review:watch-saves", "title": "watch state saves", "self_apply": "watch",
                     "applied": "added a watch"}],
                "watches": [{"type": "log", "title": "state saves failing", "contains": "state save failed",
                             "max": 3, "why": "R-261124-06"}],
                "lessons": ["one lesson"],
                "playbook": [{"rule": "a rule", "applies_to": "all", "evidence": "R-1"}]}
        data.update(extra or {})
        with open(os.path.join(d, "findings.json"), "w") as fh:
            json.dump(data, fh)
        return d

    def test_compound_answers_become_review_items_and_reingest_is_idempotent(self):
        d = self._findings()
        res = rv.ledger_ingest(DAY)
        items = rv.ledger_state()
        self.assertEqual(len(items), 3)
        by_fp = {it["fingerprint"]: it for it in items.values()}
        tool, watch = by_fp["review-trace-tool"], by_fp["review-watch-saves"]
        self.assertEqual((tool["kind"], tool["compound"], tool["status"]), ("review", True, "proposed"))
        self.assertEqual(tool["summary"], "fills+marks grepped by hand in 3 runs")      # gap -> summary
        self.assertEqual(tool["plan"], {"change": "imm_review.py trace"})
        self.assertEqual((watch["status"], watch["self_apply"]), ("open", "watch"))    # needs no one
        self.assertEqual(res["watches"]["added"], ["W-261124-01"])
        self.assertEqual(rv.ledger_stats()["compound"], 2)
        # a second ingest of the same day doubles nothing
        rv.ledger_ingest(DAY)
        self.assertEqual(len(rv.ledger_state()), 3)
        with open(os.path.join(self.review, "lessons.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("one lesson"), 1)
        with open(os.path.join(rv.MOAT_DIR, "playbook.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().count("a rule"), 1)
        self.assertEqual(len(rv.load_watches()), 1)
        ing = rv._load_json(os.path.join(d, "ingested.json"))
        self.assertEqual(len(ing["created"]), 3)                  # "new today" survives the re-ingest
        self.assertEqual(ing["watches"]["added"], ["W-261124-01"])

    def test_put_normalises_crlf_so_tables_survive(self):
        md = "# T\r\n\r\n## S\r\n| A | B |\r\n|---|---:|\r\n| x | 1 |\r\n"
        p = rv.put(DAY, "review.md", md)
        with open(p, "rb") as fh:
            self.assertEqual(fh.read().count(b"\r\r\n"), 0)
        _t, _i, secs = sr.split_review(sr._read(p))
        self.assertIn("<table", sr.md_html(secs[0][1]))

    def test_reingest_keeps_the_day_counts(self):
        self._findings({"findings": [{"fingerprint": "bug:a", "kind": "bug", "title": "a"}],
                        "compound": [], "watches": []})
        iid = rv.ledger_ingest(DAY)["ids"]["bug-a"]
        d = self._findings({"findings": [{"fingerprint": "bug:a", "kind": "bug", "title": "a"}], "compound": [],
                            "watches": [], "closures": [{"id": iid, "status": "false-positive", "note": "x"}]})
        rv.ledger_ingest(DAY)
        rv.ledger_ingest(DAY)                                         # the launcher's catch-up, say
        ing = rv._load_json(os.path.join(d, "ingested.json"))
        self.assertEqual((ing["closes"], ing["created"]), (1, [iid]))
        closes = [e for e in rv._iter_jsonl(rv.ledger_path()) if e.get("op") == "close"]
        self.assertEqual(len(closes), 1)

    def test_recheck_on_comes_due_shipped_or_not(self):
        self._findings({"findings": [{"fingerprint": "loss:rain", "kind": "loss", "title": "rain books",
                                      "recheck_on": "2026-11-30"}], "compound": [], "watches": []})
        rv.ledger_ingest(DAY)
        iid = next(iter(rv.ledger_state()))
        self.assertEqual(rv.ledger_due(date(2026, 11, 29)), [])
        due = rv.ledger_due(date(2026, 11, 30))
        self.assertEqual([(x["id"], x["why"], x["due"]) for x in due], [(iid, "recheck", "2026-11-30")])
        rv.ledger_append({"op": "eval", "id": iid, "day": "2026-11-30", "verdict": "inconclusive"})
        self.assertEqual(rv.ledger_due(date(2026, 12, 1)), [])
        # the CLI sets a new date on an existing item
        with mock.patch("sys.stdout"):
            self.assertEqual(rv.main(["ledger", "recheck", iid, "--on", "2026-12-04"]), 0)
        self.assertEqual(rv.ledger_state()[iid]["recheck_on"], "2026-12-04")
        self.assertEqual([x["why"] for x in rv.ledger_due(date(2026, 12, 4))], ["recheck"])

    def test_false_positive_closure_counts_in_precision(self):
        self._findings({"compound": [], "watches": [], "findings": [
            {"fingerprint": "bug:a", "kind": "bug", "title": "a"}, {"fingerprint": "bug:b", "kind": "bug", "title": "b"},
            {"fingerprint": "bug:c", "kind": "bug", "title": "c"}]})
        ids = rv.ledger_ingest(DAY)["ids"]
        rv.ledger_append({"op": "close", "id": ids["bug-a"], "status": "false-positive", "note": "wrong"})
        rv.ledger_append({"op": "close", "id": ids["bug-b"], "status": "closed",
                          "note": "FALSE POSITIVE: the live bot quoted all 13"})       # the 11/25 way
        rv.ledger_append({"op": "eval", "id": ids["bug-c"], "day": "2026-11-30", "verdict": "verified",
                          "delta_usd_day": 5})
        st = rv.ledger_stats()
        self.assertEqual(st["false_positives"], 2)
        self.assertEqual(st["precision"], 1.0)
        self.assertEqual(rv.ledger_state()[ids["bug-a"]]["status"], "false-positive")

    def test_watches_evaluate_each_type(self):
        s, _e = rv.et_bounds(DAY)
        stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
        self.write("incentive-mm-2026-11-24.log", "".join(
            f"{stamp(s + 60 * i)} [IMM] ! state save failed: WinError 5\n" for i in range(4))
            + f"{stamp(s - 600)} [IMM] ! state save failed: before the day\n"
            + f"{stamp(s + 900)} [IMM] fine line\n")
        self.jsonl("toxic_halts_2026-11-24.jsonl", [{"kind": "pickoff", "ts": s + 10}, {"kind": "pickoff", "ts": s + 20},
                                                    {"kind": "event", "ts": s + 30}, {"kind": "pickoff", "ts": s - 99}])
        self.write("status_incentive_mm.json", json.dumps({"errors_today": 7, "reward": {"paid": 2}}))
        old = self.write("rain_day_fair.json", "{}")
        os.utime(old, (time_now() - 5 * 3600, time_now() - 5 * 3600))
        rv._write_rows(os.path.join(rv.MOAT_DIR, "family_daily.csv"),
                       [{"day": "2026-11-23", "family": "Weather", "rew": 10, "pnl": -30, "net": -20},
                        {"day": "2026-11-24", "family": "Weather", "rew": 10, "pnl": -5, "net": 5}],
                       rv.FAMILY_DAILY_COLS)
        res = rv.watches_ingest(DAY, [
            {"type": "log", "title": "saves", "contains": "state save failed", "max": 3},
            {"type": "sink", "title": "pickoffs", "sink": "toxic_halts", "field": "kind", "equals": "pickoff", "max": 5},
            {"type": "file_age", "title": "rain fair fresh", "path": "incentive-mm/rain_day_fair.json", "max_hours": 3},
            {"type": "json", "title": "errors", "path": "incentive-mm/status_incentive_mm.json", "key": "errors_today",
             "max": 10},
            {"type": "json", "title": "a moved key", "path": "incentive-mm/status_incentive_mm.json",
             "key": "reward.gone", "max": 10},
            {"type": "family", "title": "weather net", "family": "Weather", "metric": "net", "days": 2, "min": -10},
        ], [])
        self.assertEqual(len(res["added"]), 5)                    # 5 new a run at most
        self.assertEqual(res["rejected"][0]["why"][:5], "limit")
        res2 = rv.watches_ingest(DAY + timedelta(days=1), [
            {"type": "family", "title": "weather net", "family": "Weather", "metric": "net", "days": 2, "min": -10}], [])
        self.assertEqual(len(res2["added"]), 1)
        ev = rv.eval_watches(DAY)
        st = {r["title"]: (r["state"], r.get("value")) for r in ev["results"]}
        self.assertEqual(st["saves"], ("FIRING", 4.0))            # the line before the day is not counted
        self.assertEqual(st["pickoffs"], ("ok", 2.0))
        self.assertEqual(st["rain fair fresh"][0], "FIRING")
        self.assertGreater(st["rain fair fresh"][1], 4.9)
        self.assertEqual(st["errors"], ("ok", 7.0))
        self.assertEqual(st["a moved key"], ("FIRING", None))     # a missing input fires
        self.assertEqual(st["weather net"], ("FIRING", -15.0))
        with open(os.path.join(self.review, "watch_results.jsonl")) as fh:
            n = len(fh.readlines())
        self.assertEqual(n, 6)
        rv.eval_watches(DAY, record=False)
        with open(os.path.join(self.review, "watch_results.jsonl")) as fh:
            self.assertEqual(len(fh.readlines()), n)
        self.assertEqual(rv.eval_watches(DAY)["fires"]["W-261124-01"], 1)   # a re-run day counts once
        with mock.patch.dict(os.environ, {"IMM_REVIEW_WATCHES": "0"}):
            self.assertTrue(rv.eval_watches(DAY)["off"])

    def test_watch_validation_retire_and_stale(self):
        bad = [{"type": "file_age", "title": "t", "path": "../secrets.txt", "max_hours": 1},
               {"type": "file_age", "title": "t", "path": "C:/Windows/x", "max_hours": 1},
               {"type": "log", "title": "t", "contains": "abc", "regex": "(", "max": 1},
               {"type": "log", "title": "t", "contains": "ab", "max": 1},
               {"type": "log", "title": "t", "contains": "abcd"},
               {"type": "shell", "title": "t", "max": 1},
               {"type": "json", "title": "t", "path": "incentive-mm/x.json", "key": "a;b", "max": 1}]
        res = rv.watches_ingest(DAY, bad, [])
        self.assertEqual(res["added"], [])
        self.assertEqual(len(res["rejected"]), len(bad))
        w = {"type": "log", "title": "saves", "contains": "state save failed", "max": 3}
        first = rv.watches_ingest(DAY - timedelta(days=40), [w], [])["added"][0]
        again = rv.watches_ingest(DAY, [dict(w, title="saves, renamed")], [])
        self.assertEqual((again["added"], again["updated"]), ([], [first]))   # same spec: no copy
        self.assertEqual(rv.eval_watches(DAY)["stale"], [first])             # 40 days, never fired
        rv.watches_ingest(DAY, [], [{"id": first, "note": "not needed"}])
        self.assertEqual(rv.eval_watches(DAY)["active"], 0)
        with mock.patch("sys.stdout"):
            self.assertEqual(rv.main(["watch", "list", "--all"]), 0)

    def _stream(self, day_iso, cmds, error=None, turns=40, ms=1_200_000, cost=5.5):
        d = os.path.join(self.review, day_iso)
        os.makedirs(d, exist_ok=True)
        rows = [{"type": "system", "subtype": "init"}]
        for i, c in enumerate(cmds):
            rows.append({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": c}}]}})
        rows.append({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "x"}}]}})
        if error:
            rows.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t0", "is_error": True, "content": error}]}})
        rows.append({"type": "result", "subtype": "success", "num_turns": turns, "duration_ms": ms,
                     "total_cost_usd": cost, "is_error": False, "usage": {"output_tokens": 900}})
        with open(os.path.join(d, "claude_stream.jsonl"), "w") as fh:
            fh.write("".join(json.dumps(r) + "\n" for r in rows))
        with open(os.path.join(d, "run.log"), "w", encoding="utf-8-sig") as fh:
            fh.write(f"x collect exit=0 timedout=False: evidence for {day_iso} in 1181.1s -> y\n"
                     "x === done in 52 min, exit=0 ===\n")

    def test_run_traces_and_work_done_by_hand(self):
        grep = "grep -h 'KXA' run-logs/incentive-mm/fills_2026-11-23.jsonl | python -c 'import sys'"
        snip = "python - <<'EOF'\nimport json\nopen('run-logs/incentive-mm/marks_2026-11-23.jsonl')\nEOF"
        chunk = 'F="$TEMP/f.json" && cat >> "$F" <<\'EOF\'\n{"a": 1}\nEOF'
        self._stream("2026-11-23", [grep, grep, snip, chunk, "python imm_review.py ledger list"],
                     error="Permission to use Bash with command rm -f x has been denied.")
        self._stream("2026-11-24", [grep, snip, "python imm_review.py ledger due --day 2026-11-24",
                                    "cd /c/x && git show abc --stat"])
        self.assertEqual(rv.cmd_kind(grep), "grep on fills")
        self.assertEqual(rv.cmd_kind(snip), "python snippet on marks")
        self.assertEqual(rv.cmd_kind(chunk), "assembling an output file in chunks")
        self.assertEqual(rv.cmd_kind("python imm_review.py measure --pattern x"), "imm_review measure")
        self.assertEqual(rv.cmd_kind("cd /c/x && git show abc"), "git show")
        self.assertEqual(rv.cmd_kind("grep -h x run-logs/incentive-mm/incentive-mm-2026-11-24.log"), "grep on bot log")
        tr = rv.run_trace("2026-11-23")
        self.assertEqual((tr["turns"], tr["minutes"], tr["cost_usd"]), (40, 20.0, 5.5))
        self.assertEqual((tr["bash"], tr["n_errors"], tr["denials"], tr["output_calls"]), (5, 1, 1, 1))
        self.assertEqual((tr["collect_secs"], tr["total_min"], tr["exit"]), (1181.1, 52, 0))
        self.assertEqual(tr["tools"]["Read"], 1)
        rep = rv.repeated_manual([rv.run_trace("2026-11-24"), tr])
        self.assertEqual([(x["kind"], x["runs"], x["calls"]) for x in rep],
                         [("grep on fills", 2, 3)])               # the snippet: 2 calls; ledger + git: by design
        self.assertIsNone(rv.run_trace("2026-11-20"))

    def test_self_section_outside_overlaps_memory_and_budget(self):
        # an open item about KXFOO and one about imm_health_alert.py
        rv.ledger_append({"op": "create", "id": "R-261120-01", "day": "2026-11-20", "first_seen": "2026-11-20",
                          "status": "proposed", "kind": "strategy", "fingerprint": "strategy-foo",
                          "title": "KXFOO at x5 loses money"})
        rv.ledger_append({"op": "create", "id": "R-261120-02", "day": "2026-11-20", "first_seen": "2026-11-20",
                          "status": "open", "kind": "review", "compound": True, "fingerprint": "review-alert",
                          "title": "alerts", "plan": {"files": ["imm_health_alert.py"]}})
        rv.ledger_append({"op": "decide", "id": "R-261120-01", "status": "approved", "note": "yes"})
        commits = [{"sha": "abc1234", "at": "11-24 10:00", "subject": "imm: KXFOO stands down mornings (Jack: \"yes\")",
                    "ids": [], "bot": True, "names": ["incentive_mm.py"], "body": ""},
                   {"sha": "def5678", "at": "11-24 11:00", "subject": "imm: grace", "ids": [], "bot": False,
                    "names": ["imm_health_alert.py"], "body": ""},
                   {"sha": "aaa0000", "at": "11-24 12:00", "subject": "imm: R-261120-09 done", "ids": ["R-261120-09"],
                    "bot": False, "names": [], "body": ""},
                   # bookkeeping, never a miss; and the commit that shipped R-261120-02 is no "overlap"
                   {"sha": "bbb1111", "at": "11-24 13:00", "subject": "Merge GitHub main into the box's main",
                    "ids": [], "bot": True, "names": ["incentive_mm.py", "imm_health_alert.py"], "body": ""},
                   {"sha": "ccc2222", "at": "11-24 14:00", "subject": "High-temp digest for 2026-11-23", "ids": [],
                    "bot": False, "names": [], "body": ""},
                   {"sha": "ddd3333", "at": "11-24 15:00", "subject": "imm: alert grace (review R-261120-02)",
                    "ids": ["R-261120-02"], "bot": False, "names": ["imm_health_alert.py"], "body": ""}]
        mem = os.path.join(self.root, "memory")
        os.makedirs(mem)
        s, _e = rv.et_bounds(DAY)
        for name, desc, t in (("note_a.md", "R-261120-01 re-measured: it is morning carry", s + 3600),
                              ("note_old.md", "old news", s - 86400)):
            p = os.path.join(mem, name)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(f"---\nname: {name[:-3]}\ndescription: \"{desc}\"\n---\n\nbody\n")
            os.utime(p, (t, t))
        rv.configure(status_dir=self.status, review_dir=self.review, repo=self.root, memory_dir=mem)
        self._stream("2026-11-23", ["grep x run-logs/incentive-mm/fills_2026-11-23.jsonl"])
        launcher = os.path.join(self.root, "run_imm_daily_review.ps1")
        with open(launcher, "w") as fh:
            fh.write("$CollectTimeoutMin = 35\n$ClaudeTimeoutMin = 100\n")
        ev = {"generated": time_now() - 30, "changes": {"commits": commits},
              "moat": {"_secs": 12.5}, "system": {"_secs": 40.0}}
        with mock.patch.object(rv, "HERE", self.root):
            se = rv.sec_self(DAY, ev)
        self.assertIn("compound", se["question"])
        self.assertEqual([c["sha"] for c in se["outside"]["commits"]], ["abc1234", "def5678"])
        self.assertEqual([n["file"] for n in se["outside"]["memory"]], ["note_a.md"])
        ov = {(o["id"], o["by"], o["match"]) for o in se["overlaps"]}
        self.assertIn(("R-261120-01", "commit abc1234", "KXFOO"), ov)
        self.assertIn(("R-261120-02", "commit def5678", "imm_health_alert.py"), ov)
        self.assertIn(("R-261120-01", "memory note_a.md", "names the id"), ov)
        self.assertFalse({o["by"] for o in se["overlaps"]} & {"commit bbb1111", "commit ccc2222", "commit ddd3333"})
        self.assertEqual(se["overlaps"][0]["match"], "names the id")      # a named item leads
        self.assertEqual([b["id"] for b in se["backlog"]], ["R-261120-02"])
        self.assertEqual(se["record"]["approved_unapplied"][0]["id"], "R-261120-01")
        b = se["budget"]
        self.assertEqual((b["collect_limit_s"], b["claude_limit_min"]), (2100.0, 100.0))
        self.assertEqual(b["slowest"][0], ("system", 40.0))
        self.assertGreaterEqual(b["collect_so_far_s"], 30)
        self.assertEqual(se["runs"][0]["day"], "2026-11-23")
        md = []
        rv.render_self(se, md.append)
        text = "\n".join(md)
        self.assertIn("## 11. The review itself", text)
        self.assertIn("KXFOO stands down mornings", text)
        text.encode("cp1252", errors="strict")

    def test_cycle_pass_handles_mid_file_headers_junk_and_growth(self):
        z = lambda hh, mm: datetime.fromtimestamp(_ts(DAY, hh, mm), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        u, u2 = rv.utc_dates(DAY)
        lines = ["ts,ticker,est_frac,pool_per_day",
                 f"{z(9, 0)},KXAAA-1-T1,0.10,100", "garbage line", f"{z(9, 0)},KXAAA-2-T1,0.30,nan",
                 "ts,ticker,x,pool_per_day,est_frac",                      # a restart wrote a new header
                 f"{z(9, 5)},KXAAA-1-T1,zz,100,0.50"]
        self.write(f"cycle_log_{u}.csv", "\n".join(lines) + "\n")
        nxt = self.write(f"cycle_log_{u2}.csv", "ts,ticker,est_frac,pool_per_day\n")
        self.assertEqual(len(rv.cycle_times(DAY)), 2)
        self.assertAlmostEqual(rv.cycle_share(DAY)["Fam KXAA"]["share"], (0.1 * 100 + 0.5 * 100) / 200)
        # the next UTC file keeps growing during the collect (the live bot): it was
        # written long past this day's window, so the second section reuses the scan
        with open(nxt, "a") as fh:
            fh.write(f"{z(23, 30)},KXAAA-1-T1,0.10,100\n")              # 23:30 ET: past the day
        with mock.patch.object(rv.imm_logfiles, "open_bin", side_effect=AssertionError("re-read")):
            self.assertEqual(len(rv.cycle_times(DAY)), 2)

    def test_cycle_pass_rereads_a_file_still_inside_the_window(self):
        now = time_now()
        day = datetime.fromtimestamp(now, rv.ET).date()                    # today: the window is open
        s, _e = rv.et_bounds(day)
        t1, t2 = max(now - 600, s + 1), max(now - 300, s + 2)
        z = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        u = datetime.fromtimestamp(t1, timezone.utc).date().isoformat()
        p = self.write(f"cycle_log_{u}.csv", f"ts,ticker,est_frac,pool_per_day\n{z(t1)},KXAAA-1-T1,0.1,100\n")
        self.assertEqual(len(rv.cycle_times(day)), 1)
        with open(p, "a") as fh:
            fh.write(f"{z(t2)},KXAAA-1-T1,0.1,100\n")
        self.assertEqual(len(rv.cycle_times(day)), 2)


def time_now() -> float:
    import time as _t
    return _t.time()


class CompoundEmailTests(Base):
    def setUp(self):
        super().setUp()
        self.archive(DAY, {"KXAAA-1-T1": _row(rew=50, pnl=-10)})
        self.write("status_incentive_mm.json", json.dumps({"updated_at": "2026-11-25T09:00:00Z"}))

    def _build(self, findings):
        with mock.patch.object(rv, "_ps_json", return_value=[]), mock.patch.object(rv, "_run", return_value=(0, "")):
            rv.collect(DAY, bootstrap_days=0)
        d = os.path.join(self.review, DAY.isoformat())
        with open(os.path.join(d, "review.md"), "w", encoding="utf-8") as fh:
            fh.write("# V\n\n## How the review gets better\n- two misses\n")
        with open(os.path.join(d, "findings.json"), "w") as fh:
            json.dump(findings, fh)
        rv.ledger_ingest(DAY)
        return sr.build(DAY)

    def test_compound_block_sits_outside_the_decision_cards(self):
        subject, page, text, _ = self._build({"verdict": "ok", "findings": [
            {"fingerprint": "ops:a", "kind": "ops", "severity": "P1", "title": "a", "decision_needed": True,
             "email": {"headline": "The sync is stuck"}}],
            "compound": [{"fingerprint": "review:trace", "title": "trace tool", "severity": "P1",
                          "email": {"headline": "Give the review a market timeline", "why": "hand greps",
                                    "fix": "one command", "value": "~8 turns a run"}},
                         {"fingerprint": "review:w", "title": "watch saves", "self_apply": "watch",
                          "applied": "added a state-save watch",
                          "email": {"headline": "The review watches state saves itself"}}],
            "watches": [{"type": "json", "title": "errors today", "path": "incentive-mm/status_incentive_mm.json",
                         "key": "errors_today", "max": 5}]})
        self.assertIn("1 needs your OK", subject)                        # the compound item is not a card
        self.assertIn("Making the review better", page)
        self.assertIn("Give the review a market timeline", page)
        self.assertIn("Build it: text", page)
        self.assertIn("Done by the review itself: added a state-save watch (W-261124-01)", page)
        self.assertIn("findings so far", page)
        self.assertIn("MAKING THE REVIEW BETTER (2)", text)
        self.assertIn("How the review gets better", page)                # review.md's narrative section
        self.assertLess(page.index("The sync is stuck"), page.index("Making the review better"))

    def test_tags_for_earlier_items_resolve_through_the_ledger(self):
        rv.ledger_append({"op": "create", "id": "R-261120-04", "day": "2026-11-20", "first_seen": "2026-11-20",
                          "status": "proposed", "kind": "data", "fingerprint": "data-moat-durable-backup",
                          "title": "backup"})
        _, page, text, _ = self._build({"verdict": "ok", "findings": []})
        d = os.path.join(self.review, DAY.isoformat())
        with open(os.path.join(d, "review.md"), "w", encoding="utf-8") as fh:
            fh.write("# V\n\n## Still waiting on you\n- the backup [[fp:data:moat-durable-backup]] waits\n")
        _, page, text, _ = sr.build(DAY)
        self.assertIn("the backup R-261120-04 waits", text)

    def test_a_day_without_an_answer_says_so(self):
        _, page, text, _ = self._build({"verdict": "ok", "findings": []})
        self.assertIn("did not answer its daily question", page)
        self.assertIn("did not answer its daily question", text)
        evj = rv._load_json(os.path.join(self.review, DAY.isoformat(), "evidence.json"))
        self.assertIn("self", evj)
        with open(os.path.join(self.review, DAY.isoformat(), "evidence.md"), encoding="utf-8") as fh:
            self.assertIn("## 11. The review itself", fh.read())


class RegistryTests(Base):
    """The change registry (section 12): every IMM change, measured at 7 days."""

    FAMS = {"KXVERA": ("AI & tech", "Vercel shares"), "KXVERB": ("AI & tech", "Vercel shares"),
            "KXSPA": ("Sports & awards", "Sports ladders")}

    def setUp(self):
        super().setUp()
        rv._FAM_FN = lambda s: self.FAMS.get(s, ("Fam " + s[:4], s))

    def repo_file(self, name, text=""):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_bot_modules_and_change_kinds(self):
        self.repo_file("incentive_mm.py", "import os\nimport foo_fair\n    from bar import x\ndef f():\n"
                                          "    import baz as b, qux\n    import imm_dashboard\n"
                                          '"""\nfrom the docstring\n"""\n')
        self.repo_file("foo_fair.py", "import kalshi_lib\n")
        for n in ("bar.py", "baz.py", "kalshi_lib.py", "imm_dashboard.py", "email_mod.py"):
            self.repo_file(n)
        self.repo_file("pvd_scan.py", "import email_mod\n")          # a writer's imports are not followed
        self.assertEqual(rv.bot_modules(), {"foo_fair.py", "bar.py", "baz.py", "kalshi_lib.py"})
        self.assertEqual(rv.change_kind(["incentive_mm.py", "test_incentive_mm.py"]), "bot")
        self.assertEqual(rv.change_kind(["run_incentive_mm.ps1"]), "launcher")
        self.assertEqual(rv.change_kind(["kalshi_lib.py"]), "input")
        self.assertEqual(rv.change_kind(["pvd_scan.py", "test_pvd_scan.py"]), "input")
        self.assertIsNone(rv.change_kind(["email_mod.py"]))
        self.assertIsNone(rv.change_kind(["imm_dashboard.py", "imm_review.py"]))
        self.assertIsNone(rv.change_kind(["README.md", ".claude/skills/x/SKILL.md"]))

    def test_scope_from_what_the_commit_names(self):
        al = rv._family_aliases(["KXVERA", "KXVERB", "KXSPA"])
        self.assertEqual(al["vercel"], ("groups", "Vercel shares"))
        self.assertEqual(al["sports"], ("families", "Sports & awards"))
        s = rv.infer_scope("imm: the RCP weekly (KXAPRPOTUS) stands down; Vercel too", "", al)
        self.assertEqual(s["series"], ["KXAPRPOTUS"])
        self.assertNotIn("groups", s)                 # a named series is not widened to a family
        self.assertEqual(rv.infer_scope("imm: the Vercel bid-edge margin 3c -> 2c", "", al)["groups"],
                         ["Vercel shares"])
        s = rv.infer_scope("imm: sports ladders x4", "", al)
        self.assertEqual(s["groups"], ["Sports ladders"])  # the longest alias wins
        self.assertNotIn("families", s)
        self.assertEqual(rv.infer_scope("imm: RT ladder lean x2.25", "", al)["series"], ["KXRT"])
        s = rv.infer_scope("imm: a held market's close is never held back", "e.g. KXFOO-26 and KXBAR", al)
        self.assertEqual(s["series"], ["KXFOO", "KXBAR"])
        self.assertIn("body", s["basis"])
        self.assertEqual(rv.infer_scope("pvd waves: --cost-mult", "the w1111-elec2028 long shots", al)["waves"],
                         ["w1111-elec2028"])
        self.assertTrue(rv.infer_scope("imm: state save retries", "", al)["book"])
        sc, why = rv.clean_scope({"series": "KXA, KXB", "families": "Elections"})
        self.assertEqual((sc["series"], sc["families"]), (["KXA", "KXB"], ["Elections"]))
        self.assertIsNone(rv.clean_scope({"pattern": "("})[0])
        self.assertIsNone(rv.clean_scope({})[0])

    def test_live_times(self):
        A, B, C, D = "1" * 40, "2" * 40, "3" * 40, "4" * 40
        order, parents = [D, C, B, A], {D: [C], C: [B], B: [A], A: []}
        reflog = [(100, A), (200, B), (300, D)]                       # C reached main with D
        runs = [{"ts": 150.0, "run_id": "r1", "sha": A[:7], "src": 90.0},
                {"ts": 260.0, "run_id": "r2", "sha": B[:7], "src": 250.0},   # the file changed after B landed
                {"ts": 400.0, "run_id": "r3", "sha": D[:7], "src": 300.0}]
        commits = [{"full": A, "kind": "bot", "ct": 95}, {"full": B, "kind": "bot", "ct": 190},
                   {"full": C, "kind": "bot", "ct": 280}, {"full": D, "kind": "input", "ct": 290}]
        live = rv.resolve_live(order, parents, reflog, runs, commits)
        self.assertEqual(live[A][0], 150.0)
        self.assertEqual(live[B][0], 260.0)
        self.assertEqual(live[C][0], 260.0)           # the run on its uncommitted edits, not r3
        self.assertIn("before they were committed", live[C][1])
        self.assertEqual(live[D][0], 300.0)           # an input writer: when main got it
        self.assertEqual(rv.resolve_live(order, parents, reflog, runs[:1], commits[2:3]), {})   # not live yet

    def test_env_changes_skip_reordered_sets_and_secrets(self):
        runs = [{"ts": 1.0, "run_id": "r1", "sha": "x", "config": {
                    "IMM_A": "1", "IMM_SET": "frozenset({'X', 'Y'})", "IMM_HEARTBEAT_URL": "u1", "code:Z": 1}},
                {"ts": 2.0, "run_id": "r2", "sha": "x", "config": {
                    "IMM_A": "2", "IMM_SET": "frozenset({'Y', 'X'})", "IMM_HEARTBEAT_URL": "u2", "code:Z": 2,
                    "IMM_BLOCK": "['KXFOO', 'KXBAR']"}}]
        ch = {c["key"]: c for c in rv.env_changes(runs)}
        self.assertEqual(sorted(ch), ["env:IMM_A@r2", "env:IMM_BLOCK@r2"])
        self.assertEqual(ch["env:IMM_A@r2"]["change"], "1 -> 2")
        self.assertEqual(ch["env:IMM_BLOCK@r2"]["tokens"], ["KXBAR", "KXFOO"])

    def _days(self, split, before, after, extra=None):
        for i in range(1, 8):
            self.archive(split - timedelta(days=i), {**before(i), **(extra or {})})
            self.archive(split + timedelta(days=i), {**after(i), **(extra or {})})

    def test_readout_and_its_call(self):
        S = DAY - timedelta(days=8)
        self._days(S, lambda i: {"KXAA-E1-T1": _row(rew=10 + i % 2, pnl=-5, rest=1000)},
                   lambda i: {"KXAA-E1-T1": _row(rew=20 + i % 2, pnl=5, rest=1000)},
                   {"KXZZ-E-T1": _row(rew=50)})
        self.jsonl("program_history.jsonl", [{"date": (S + timedelta(days=i)).isoformat(), "series": "KXAA",
                                              "total_reward_raw": 1e6} for i in range(-7, 8) if i])
        end = S + timedelta(days=7)
        r = rv.change_readout({"series": ["KXAA"]}, S, 7, end)
        self.assertEqual((r["before_days"], r["after_days"]), (7, 7))
        self.assertAlmostEqual(r["delta"]["net"], 20.0, places=1)
        self.assertEqual(r["call"], "better")
        self.assertAlmostEqual(r["outside"]["delta"], 0.0)
        self.assertEqual(r["supply_ratio"], 1.0)
        self.assertEqual(rv.change_readout({"book": True}, S, 7, end)["call"], "book-wide")
        self.assertEqual(rv.change_readout({"series": ["KXNONE"]}, S, 7, end)["call"], "no activity")
        self.assertEqual(rv.change_readout({"series": ["KXAA"]}, S, 7, end, metric="fills")["call"],
                         "no clear change")
        self.assertEqual(rv.change_readout({"series": ["KXAA"]}, S, 7, S + timedelta(days=3))["after_days"], 3)

    def test_exact_permutation_p_fdr_and_power(self):
        self.assertEqual(rv.perm_p([0.0] * 7, [1.0] * 7), round(2 / 3432, 5))   # the two extreme splits of 3,432
        self.assertEqual(rv.perm_p([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]), 1.0)
        self.assertIsNone(rv.perm_p([1.0], [2.0, 3.0]))
        big = rv.perm_p([0.0, 1.0] * 8, [5.0, 6.0] * 8)                            # C(32,16): sampled, seeded
        self.assertLess(big, 0.001)
        self.assertEqual(big, rv.perm_p([0.0, 1.0] * 8, [5.0, 6.0] * 8))
        self.assertEqual(rv.bh_qvalues([0.01, 0.04, 0.03, 0.2, None]), [0.04, 0.05333, 0.05333, 0.2, None])
        a, b = [0, 2, 0, 2, 0, 2, 0], [1, 3, 1, 3, 1, 3, 1]
        import statistics as st_
        self.assertAlmostEqual(rv.mde(a, b), round(2.8 * math.sqrt(st_.variance(a) / 7 + st_.variance(b) / 7), 2))
        r = {"before_days": 7, "after_days": 7, "metric": "net", "p": 0.01,
             "before": {"net": 5, "fills": 2, "rest": 500}, "after": {"net": 25}, "delta": {"net": 20}}
        self.assertEqual(rv.change_call(r), "better")                   # a lone readout
        self.assertEqual(rv.change_call(dict(r, fdr_ok=False)), "no clear change")   # fails the batch's FDR
        self.assertEqual(rv.change_call(dict(r, p=0.2)), "no clear change")

    def test_overlapping_readouts_count_once(self):
        """The 11/26 headless run: two weekend-rain readouts were the same -$63/day."""
        rs = [{"split": "2026-11-10", "group": "g1", "keys": ["a"], "_fp": frozenset({"KXAA"}), "p": 0.01},
              {"split": "2026-11-10", "group": "g2", "keys": ["b"], "_fp": frozenset({"KXAA", "KXBB"}), "p": 0.02},
              {"split": "2026-11-11", "group": "g3", "keys": ["c"], "_fp": frozenset({"KXAA"}), "p": 0.03},
              {"split": "2026-11-10", "group": "g4", "keys": ["d"], "_fp": frozenset({"KXCC"}), "p": 0.04},
              {"split": "2026-11-10", "group": "g5", "keys": ["e"], "_fp": None, "p": None}]
        rv.registry_clusters(rs)
        self.assertEqual((rs[0]["same_money_as"], rs[0]["cluster"]), (["b"], "g2"))  # no call: the widest leads
        self.assertNotIn("same_money_as", rs[1])
        self.assertNotIn("same_money_as", rs[2])                     # the next day: its own window
        self.assertNotIn("same_money_as", rs[3])                     # other series
        self.assertNotIn("cluster", rs[4])                           # whole book: never merged
        # a narrow change that was called leads a wider one that was not
        rs2 = [{"split": "2026-11-10", "group": "w", "keys": ["wide"], "_fp": frozenset({"KXAA", "KXBB", "KXCC"}),
                "p": 0.6, "call": "no clear change"},
               {"split": "2026-11-10", "group": "n", "keys": ["narrow"], "_fp": frozenset({"KXAA"}), "p": 0.004,
                "q": 0.05, "call": "worse", "metric": "net", "delta": {"net": -63.0}}]
        rv.registry_clusters(rs2)
        self.assertEqual(rs2[0]["same_money_as"], ["narrow"])
        self.assertNotIn("same_money_as", rs2[1])
        st = {"a": {"kind": "bot", "readouts": {"7": {"group": "g1", "result": {
                  "call": "worse", "metric": "net", "delta": {"net": -60.0}, "same_money_as": ["b"]}}}},
              "b": {"kind": "bot", "readouts": {"7": {"group": "g2", "result": {
                  "call": "worse", "metric": "net", "delta": {"net": -63.0}}}}}}
        b = rv.registry_board(st)
        self.assertEqual((b["calls"], b["worse_usd_day"], b["merged"], b["read_out"]), ({"worse": 1}, -63.0, 1, 2))

    def test_registry_off_switch(self):
        with mock.patch.dict(os.environ, {"IMM_REVIEW_REGISTRY": "0"}), \
                mock.patch.object(rv, "_ps_json", return_value=[]), mock.patch.object(rv, "_run", return_value=(0, "")):
            ev = rv.collect(DAY, only=["registry"])
        self.assertNotIn("registry", ev)
        a = []
        rv.render_registry({}, a.append)
        self.assertIn("IMM_REVIEW_REGISTRY=0", "\n".join(a))

    def test_small_moves_and_new_scopes(self):
        S = DAY - timedelta(days=8)
        self._days(S, lambda i: {"KXAA-E1-T1": _row(rew=100 + i % 2, rest=1000)},
                   lambda i: {"KXAA-E1-T1": _row(rew=103 + i % 2, rest=1000),       # +3/day: under $5
                              "KXNEW-E1-T1": _row(rew=30 + i % 3, rest=500)})
        end = S + timedelta(days=7)
        self.assertEqual(rv.change_readout({"series": ["KXAA"]}, S, 7, end)["call"], "no clear change")
        self.assertEqual(rv.change_readout({"series": ["KXNEW"]}, S, 7, end)["call"], "new, better")

    # an end-to-end registry: git (mocked), bot runs, archives
    def _seed(self):
        self.S = S = DAY - timedelta(days=8)
        self.P, self.C1, self.C2, self.C3 = P, C1, C2, C3 = "a" * 40, "b" * 40, "c" * 40, "d" * 40
        self.k1, self.k3 = C1[:7], C3[:7]
        ct = {P: int(_ts(DAY - timedelta(days=20))), C1: int(_ts(S, 10, 0)), C2: int(_ts(S, 11, 0)),
              C3: int(_ts(DAY, 9, 0))}
        meta = [(C3, "imm launcher: collateral budget 100 -> 200 (Jack 2026-11-24: \"yes\")", "",
                 ["run_incentive_mm.ps1"]),
                (C2, "docs: the handoff", "", ["INCENTIVE_MM_HANDOFF.md"]),
                (C1, "imm: KXAA quotes x2 (Jack 2026-11-16: \"yes\")", "", ["incentive_mm.py", "test_incentive_mm.py"])]
        self.log_out = "".join(f"\x1e{h}\x1f{ct[h]}\x1f{s}\x1f{b}\x1d\n" + "\n".join(f) + "\n" for h, s, b, f in meta)
        self.rev_out = f"{C3} {C2}\n{C2} {C1}\n{C1} {P}\n{P}\n"
        self.reflog_out = "".join(f"{h}\x1fmain@{{{ct[h]}}}\n" for h in (C3, C2, C1, P))
        runs = [(_ts(S - timedelta(days=1)), "r0", P, ct[P], "100"),
                (_ts(S, 10, 3), "r1", C1, _ts(S, 10, 0), "100"),
                (_ts(DAY, 9, 2), "r2", C3, _ts(S, 10, 0), "200")]
        by_day = defaultdict(list)
        for ts, rid, h, src, budget in runs:
            u = datetime.fromtimestamp(ts, timezone.utc)
            by_day[u.date().isoformat()].append({"ts": u.isoformat(), "run_id": rid, "git_sha": h[:7],
                                                 "source_mtime": src,
                                                 "config": {"IMM_COLLATERAL_BUDGET": budget}})
        for u, rows in by_day.items():
            self.jsonl(f"config_history_{u}.jsonl", rows)
        self._days(S, lambda i: {"KXAA-E1-T1": _row(rew=10 + i % 2, rest=1000)},
                   lambda i: {"KXAA-E1-T1": _row(rew=30 + i % 2, rest=1000)}, {"KXZZ-E-T1": _row(rew=50)})
        self.now = _ts(DAY + timedelta(days=1), 4)

    def fake_git(self, *args, timeout=60):
        return {"log": (0, self.log_out), "rev-list": (0, self.rev_out),
                "reflog": (0, self.reflog_out)}.get(args[0], (1, ""))

    def _sec(self, day=DAY, now=None):
        with mock.patch.object(rv, "_git", side_effect=self.fake_git):
            return rv.sec_registry(day, now=now or self.now)

    def test_registers_resolves_and_reads_out(self):
        self._seed()
        rg = self._sec()
        st = rv.registry_state()
        env = "env:IMM_COLLATERAL_BUDGET@r2"
        self.assertTrue(rg["sync"]["first"])
        self.assertEqual(sorted(st), sorted([self.k1, self.k3, env]))          # not the docs commit
        self.assertEqual(st[self.k1]["kind"], "bot")
        self.assertEqual(st[self.k1]["scope"]["series"], ["KXAA"])
        self.assertAlmostEqual(st[self.k1]["live_at"], round(_ts(self.S, 10, 3), 1))
        self.assertAlmostEqual(st[self.k3]["live_at"], round(_ts(DAY, 9, 2), 1))
        self.assertIn("launcher commit " + self.k3, st[env]["skipped"])         # one change, measured once
        self.assertEqual(rg["n_readouts"], 1)
        r = rg["readouts"][0]
        self.assertEqual((r["keys"], r["call"]), ([self.k1], "better"))
        self.assertAlmostEqual(r["delta"]["net"], 20.0, places=1)
        self.assertEqual((r["p"], r["q"], r["fdr_ok"]), (round(2 / 3432, 5), round(2 / 3432, 5), True))
        self.assertIsNotNone(r["mde"])
        self.assertFalse(r["expect_pre"])                                       # nothing was registered
        self.assertEqual(sorted(w["key"] for w in rg["went_live"]), sorted([self.k3, env]))
        self.assertEqual(rg["board"]["read_out"], 1)
        self.assertEqual(rg["next_due"], [])
        # an expectation written the day it went live is pre-registered; a second
        # collect of the day recomputes and registers nothing new
        rv.registry_ingest(self.S, [{"sha": self.k1, "expect": "KXAA net up with x2 size"}])
        rg2 = self._sec()
        self.assertEqual((rg2["sync"]["registered"], rg2["n_readouts"]), ([], 1))
        self.assertTrue(rg2["readouts"][0]["expect_pre"])
        # the next day: nothing due again
        self.assertEqual(self._sec(DAY + timedelta(days=1), self.now + 86400)["n_readouts"], 0)
        a = []
        rv.render_registry(rg, a.append)
        md = "\n".join(a)
        self.assertIn("## 12. Change registry", md)
        self.assertIn(self.k1, md)
        self.assertIn("better", md)

    def test_ingest_scopes_judges_and_extends(self):
        self._seed()
        self._sec()
        d = os.path.join(self.review, DAY.isoformat())
        rv._write_json(os.path.join(d, "findings.json"), {"findings": [], "changes": [
            {"sha": self.k1, "verdict": "worked", "note": "x2 paid for itself"},
            {"sha": self.k3, "scope": {"book": True}, "metric": "rew", "expect": "more rewards for the collateral"},
            {"sha": "deadbee", "verdict": "worked"},
            {"sha": self.k1, "verdict": "great"}]})
        res = rv.ledger_ingest(DAY)["changes"]
        self.assertEqual((res["judged"], res["scoped"], len(res["rejected"])), ([self.k1], [self.k3], 2))
        n = len(list(rv._iter_jsonl(rv.registry_path())))
        rv.ledger_ingest(DAY)                                     # idempotent
        self.assertEqual(len(list(rv._iter_jsonl(rv.registry_path()))), n)
        st = rv.registry_state()
        self.assertEqual(st[self.k1]["verdict"], "worked")
        self.assertEqual((st[self.k3]["metric"], st[self.k3]["scope_by"]), ("rew", "review"))
        # a scope changed after its readout is read out again
        nxt = DAY + timedelta(days=1)
        self.assertFalse(rv._registry_due(st[self.k1], nxt))
        rv.registry_ingest(nxt, [{"sha": self.k1, "scope": {"series": ["KXZZ"]}}])
        self.assertTrue(rv._registry_due(rv.registry_state()[self.k1], nxt))
        # extend: read again at 14 days
        rv.registry_ingest(nxt, [{"sha": self.k1, "verdict": "extend", "note": "confounded"}])
        it = rv.registry_state()[self.k1]
        self.assertEqual(rv._horizon(it), rv.REGISTRY_EXTEND_DAYS)
        self.assertFalse(rv._registry_due(it, self.S + timedelta(days=13)))
        self.assertTrue(rv._registry_due(it, self.S + timedelta(days=14)))

    def test_confounds_and_early_warning(self):
        self._seed()
        # a second change on KXAA three days after the first, and one that is crashing now
        C4 = "e" * 40
        ct4 = int(_ts(self.S + timedelta(days=3), 9))
        self.log_out = f"\x1e{C4}\x1f{ct4}\x1fimm: KXAA asks x3\x1f\x1d\nincentive_mm.py\n" + self.log_out
        # main: P - C1 - C2 - C4 - C3
        self.rev_out = (f"{self.C3} {C4}\n{C4} {self.C2}\n{self.C2} {self.C1}\n{self.C1} {self.P}\n{self.P}\n")
        self.reflog_out = f"{C4}\x1fmain@{{{ct4}}}\n" + self.reflog_out
        self.jsonl(f"config_history_{(self.S + timedelta(days=3)).isoformat()}.jsonl",
                   [{"ts": datetime.fromtimestamp(ct4 + 120, timezone.utc).isoformat(), "run_id": "r9",
                     "git_sha": C4[:7], "source_mtime": ct4, "config": {"IMM_COLLATERAL_BUDGET": "100"}}])
        rg = self._sec()
        r = next(x for x in rg["readouts"] if x["keys"] == [self.k1])
        self.assertEqual([c["key"] for c in r["confounds"]], [C4[:7]])
        self.assertEqual(r["confounds"][0]["side"], "after")
        self.assertEqual(r["clean_days"], 2)

    def test_email_block(self):
        self._seed()
        rg = self._sec()
        d = os.path.join(self.review, DAY.isoformat())
        rv._write_json(os.path.join(d, "evidence.json"), {"day": DAY.isoformat(), "registry": rg})
        rv._write_json(os.path.join(d, "findings.json"), {"verdict": "ok", "findings": [], "changes": [
            {"sha": self.k1, "verdict": "worked", "note": "x2 paid for itself"}]})
        with open(os.path.join(d, "review.md"), "w", encoding="utf-8") as fh:
            fh.write("# V\n\n## What happened\n- fine\n")
        rv.ledger_ingest(DAY)
        _, page, text, _ = sr.build(DAY)
        block = page[page.index("Changes measured at 7 days"):]
        self.assertIn("KXAA quotes x2", block)
        self.assertNotIn("Jack 2026-11-16", block[:2500])          # the quote is cut from the title
        self.assertIn("Review: worked", block)
        self.assertIn("none of 1 worse", page)                     # the health chip
        self.assertIn("CHANGES MEASURED AT 7 DAYS (1)", text)
        self.assertIn("review: worked - x2 paid for itself", text)
        self.assertEqual(sr._change_title('imm waves: --cost-mult (Jack 2026-11-26: "5x only")'), "Waves: --cost-mult")
        self.assertEqual(sr._change_title("imm fair lead: within 5c of the model"), "Fair lead: within 5c of the model")
        self.assertEqual(sr._change_title("imm: the RCP weekly stands down (review R-261125-01)"),
                         "The RCP weekly stands down")
        self.assertEqual(sr._change_title("wave_fair: the weekday FX anchor"), "wave_fair: the weekday FX anchor")
        self.assertEqual(sr._change_title("imm Trump mentions: both halved"), "Trump mentions: both halved")
        self.assertEqual(sr._change_title("the Vercel bid-edge: 2c"), "The Vercel bid-edge: 2c")
        self.assertEqual(sr._change_title("env IMM_A: 1 -> 2"), "Setting IMM_A: 1 -> 2")

    def test_email_mixes_worse_and_better_when_crowded(self):
        rows = ([{"call": "worse", "verdict": "", "n": i} for i in range(7)]
                + [{"call": "better", "verdict": "", "n": 7 + i} for i in range(3)]
                + [{"call": "no clear change", "verdict": "worked", "n": 10}])
        got = [r["n"] for r in sr.pick_changes(rows)]
        self.assertEqual(got, [0, 1, 2, 3, 4, 7, 8, 9])         # 5 worse, then 3 better, in order
        self.assertEqual(len(sr.pick_changes(rows[:4])), 4)       # all of them when they fit


class BackupStatusTests(Base):
    """The moat's BigQuery backup as the review sees it (R-261124-20)."""

    def test_status_render_and_chip(self):
        import time as _t
        self.assertEqual(rv.backup_status()["state"], "never run")
        path = os.path.join(self.review, "backup_state.json")
        os.makedirs(self.review, exist_ok=True)
        rv._write_json(path, {"at": rv._now_iso(), "ok": True, "dataset": "p:imm_review",
                              "added": {"lines": {"ledger_lines": 2}, "files": 3, "days_changed": 1},
                              "totals": {"files": 3, "day_archive": 1, "ledger_lines": 2, "changes_lines": 0}})
        ok = rv.backup_status()
        self.assertEqual((ok["state"], ok["totals"]["files"]), ("ok", 3))
        self.assertEqual(rv.backup_status(now=_t.time() + 40 * 3600)["state"], "stale")
        self.assertIn(("Moat backed up", "green"), sr.health_checks({"moat": {"backup": ok}}))
        rv._write_json(path, {"at": rv._now_iso(), "ok": False,
                              "refused": ["ledger_lines: daily_review/ledger.jsonl has 1 lines, the copy 2"]})
        bad = rv.backup_status()
        self.assertEqual(bad["state"], "FAILED")
        self.assertIn(("Moat backup FAILED", "red"), sr.health_checks({"moat": {"backup": bad}}))
        md = rv.render_evidence({"day": DAY.isoformat(), "window_et": ["a", "b"], "generated": _t.time(),
                                 "moat": {"assets": {}, "priors_book": {}, "backup": bad}})
        self.assertIn("**Backup** (BigQuery", md)
        self.assertIn("backup problem: ledger_lines", md)
        self.assertIn("raise it as P1", md)
        with open(path, "w") as fh:
            fh.write('{"at": "2026-11-26T00:00:00Z", "off": true, "ok": true}')
        self.assertEqual(rv.backup_status()["state"], "off")


if __name__ == "__main__":
    unittest.main()
