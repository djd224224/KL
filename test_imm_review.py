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
        dh = rv.sec_data(DAY)
        st = {r["sink"]: r["status"] for r in dh["sinks"]}
        self.assertEqual(st["orders"], "LOW")
        self.assertEqual(st["marks"], "MISSING")
        self.assertNotIn("imm_toxic_halts_sent", st)

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

    def test_send_substitutes_ids_and_appends_numbers(self):
        self._collect()
        d = os.path.join(self.review, DAY.isoformat())
        with open(os.path.join(d, "review.md"), "w", encoding="utf-8") as fh:
            fh.write("# Verdict\n\n1. Fix the sync [[fp:ops:sync-wedged]]\n")
        with open(os.path.join(d, "findings.json"), "w") as fh:
            json.dump({"subject": "net +$40 | 1 decision", "findings": [
                {"fingerprint": "ops:sync-wedged", "kind": "ops", "title": "t", "decision_needed": True}]}, fh)
        rv.ledger_ingest(DAY)
        subject, page, text = sr.build(DAY)
        self.assertIn("R-261124-01", text)
        self.assertNotIn("[[fp:", text)
        self.assertIn("The numbers (generated from the evidence pack", text)
        self.assertTrue(subject.startswith("IMM review Tue 11/24: net +$40"))
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
        subject, page, text = sr.build(DAY, fallback=True, reason="claude exit 1")
        self.assertIn("DATA ONLY (claude exit 1)", subject)
        self.assertIn("The analysis did not run (claude exit 1)", text)
        self.assertIn("## 1. Scorecard", text)
        s2, _, t2 = sr.build(DAY)                                          # no review.md at all
        self.assertIn("DATA ONLY (review.md missing)", s2)

    def test_dry_run_writes_html_only(self):
        self._collect()
        with mock.patch.object(sr, "send") as snd:
            self.assertEqual(sr.main(["--day", DAY.isoformat(), "--dry", "--fallback"]), 0)
        snd.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(self.review, DAY.isoformat(), "review.html")))


if __name__ == "__main__":
    unittest.main()
