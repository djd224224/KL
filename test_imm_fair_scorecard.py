"""Tests for imm_fair_marks.py (the 15-minute fair log) and imm_fair_scorecard.py
(the daily model scorecard, evidence section 13). A synthetic run-logs tree in a
temp dir; a fake stands in for Kalshi. Nothing reads or writes the live tree."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import imm_fair_marks as fm
import imm_fair_scorecard as fs
import imm_review as rv
import send_imm_review as sr

DAY = date(2026, 11, 24)


def et_ts(day, hh=12, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=rv.ET).timestamp()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.status = os.path.join(self.root, "run-logs", "incentive-mm")
        self.review = os.path.join(self.status, "daily_review")
        os.makedirs(self.review)
        rv.configure(status_dir=self.status, review_dir=self.review, repo=self.root)

    def tearDown(self):
        rv.configure()
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, name, text):
        p = os.path.join(self.status, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        return p

    def jsonl(self, name, rows):
        return self.write(name, "".join(json.dumps(r) + "\n" for r in rows))

    def lines(self, name):
        p = os.path.join(self.status, name)
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]


class SnapshotterTests(Base):
    def test_extract_and_family_names(self):
        d = {"generated_at": "2026-11-24T10:00:00Z", "markets": {
            "A-1": {"p": 0.42}, "A-2": {"fair": 0.1, "fetched_at": "x"}, "A-3": {"p": 0.5, "err": "bad"},
            "A-4": {"p": None}, "A-5": {"p": True}, "A-6": {"p": 1.7}, "A-7": "junk"}}
        got = fm.extract(d)
        self.assertEqual(got, {"A-1": (0.42, "2026-11-24T10:00:00Z"), "A-2": (0.1, "x"), "A-6": (1.0, "2026-11-24T10:00:00Z")})
        self.assertEqual(fm.extract({"entries": []}), {})
        self.assertEqual([fm.family_of_file(x) for x in ("approval_fair.json", "rain_fair_values.json",
                                                         "home_value_fair.json", "yt_top1_fair.json")],
                         ["approval", "rain", "home_value", "yt_top1"])

    def test_books_from_the_cycle_log_tail(self):
        now = et_ts(DAY, 15)
        u = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        head = "ts,ticker,ext_bid,ext_ask,yes_depth\n"
        rows = [f"{iso(now - 3000)},KXA-1,10,20,5", f"{iso(now - 300)},KXA-1,12,18,5",
                f"{iso(now - 60)},KXB-1,,40,5", f"{iso(now - 30)},KXC-1,30"]          # stale, newest, one side, partial
        self.write(f"cycle_log_{u}.csv", head + "\n".join(rows) + "\n")
        b = fm.latest_books(now)
        self.assertEqual(b["KXA-1"][:2], (12.0, 18.0))
        self.assertEqual(b["KXB-1"][:2], (None, 40.0))
        self.assertNotIn("KXC-1", b)

    def _setup_files(self, now, p1=0.40, keep_b=True):
        u = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        self.write(f"cycle_log_{u}.csv", f"ts,ticker,ext_bid,ext_ask\n{iso(now - 120)},KXAPR-1,38,42\n")
        mk = {"KXAPR-1": {"p": p1}}
        if keep_b:
            mk["KXAPR-2"] = {"p": 0.9}
        self.write("approval_fair.json", json.dumps({"generated_at": iso(now), "markets": mk}))
        self.write("vercel_fair.json", json.dumps({"at": iso(now), "entries": [{"k": 1}]}))

    def test_runs_marks_books_raw_and_results(self):
        now = et_ts(DAY, 15)
        self._setup_files(now)
        calls = []

        def fake(tickers):
            calls.append(list(tickers))
            out = {t: {"bid": 85.0, "ask": 95.0, "status": "active", "result": "", "close_ts": None, "type": "binary"}
                   for t in tickers if t == "KXAPR-2"}
            out.update({t: {"bid": None, "ask": None, "status": "finalized", "result": "yes",
                            "close_ts": now - 100, "type": "binary"} for t in tickers if t == "KXGONE-1"})
            return out

        o = fm.run(now=now, markets_fn=fake)
        self.assertEqual((o["marks"], o["markets"], o["api_books"], o["raw_files"]), (2, 2, 1, ["vercel"]))
        u = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        marks = {m["ticker"]: m for m in self.lines(f"fair_marks_{u}.jsonl")}
        self.assertEqual((marks["KXAPR-1"]["bid"], marks["KXAPR-1"]["book_src"]), (38.0, "cycle"))
        self.assertEqual((marks["KXAPR-2"]["ask"], marks["KXAPR-2"]["book_src"]), (95.0, "api"))
        self.assertTrue(os.path.exists(os.path.join(self.status, f"fair_raw_{u}.jsonl.gz")))
        self.assertEqual(calls, [["KXAPR-2"]])                     # hourly quotes for the uncycled market only
        # a minute later: nothing moved, no new marks, no new reads, no new raw copy
        o = fm.run(now=now + 60, markets_fn=fake)
        self.assertEqual((o["marks"], o["reads"], o["raw_files"]), (0, 0, []))
        # the fair moves 2pp: a mark; KXAPR-2 leaves the file and a gone market settles
        st = fm.load_state()
        st["cur"]["KXGONE-1"] = [now - 3600, 0.3, "approval"]
        rv._write_json(fm.state_path(), st)
        self._setup_files(now + 120, p1=0.42, keep_b=False)
        o = fm.run(now=now + 120, markets_fn=fake)
        self.assertEqual(o["marks"], 1)
        res = self.lines(f"fair_results_{u}.jsonl")
        self.assertEqual([(r["ticker"], r["result"], r["family"]) for r in res], [("KXGONE-1", "yes", "approval")])
        st = fm.load_state()
        self.assertIn("KXGONE-1", st["done"])
        self.assertNotIn("KXGONE-1", st["cur"])
        self.assertIn("KXAPR-2", st["lookup"])                     # looked up, not settled: retried in 6h

    def test_give_up_dry_run_and_off_switch(self):
        now = et_ts(DAY, 15)
        self._setup_files(now)
        rv._write_json(fm.state_path(), {"cur": {"OLD-1": [now - 15 * 86400, 0.5, "approval"]}})
        o = fm.run(now=now, dry=True, markets_fn=lambda t: {})
        self.assertEqual(o["given_up"], 1)
        u = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        self.assertFalse(os.path.exists(os.path.join(self.status, f"fair_marks_{u}.jsonl")))
        with mock.patch.dict(os.environ, {"IMM_FAIR_MARKS": "0"}):
            self.assertTrue(fm.run(now=now)["off"])

    def test_a_crash_or_an_empty_read_is_a_failed_line_the_review_reports(self):
        # pythonw has no console: the task log is the only place a crash shows
        log = os.path.join(self.status, "fair-marks-task.log")
        now = datetime.now(timezone.utc).timestamp()
        self._setup_files(now)
        with mock.patch.object(fm, "kalshi_markets", return_value={}):
            self.assertEqual(fm.main(["run", "--log", log]), 0)
        with mock.patch.object(fm, "run", side_effect=RuntimeError("boom")):
            self.assertEqual(fm.main(["run", "--log", log]), 2)
        with mock.patch.object(fm, "run", return_value={"at": iso(now), "files": 0, "marks": 0, "families": {}}):
            self.assertEqual(fm.main(["run", "--log", log]), 1)
        with open(log, encoding="utf-8") as fh:
            ok, crash, empty = [json.loads(l) for l in fh if l.strip()]
        self.assertNotIn("status", ok)
        self.assertEqual(ok["marks"], 2)
        self.assertEqual(crash["status"], "FAILED")
        self.assertIn("RuntimeError: boom", crash["error"])
        self.assertIn("Traceback", crash["trace"])
        self.assertTrue(empty["status"].startswith("FAILED"))
        # the review's task-log scan reads this log and reports both failures, not the good run
        day = datetime.fromtimestamp(now, timezone.utc).date()
        hits = [h for h in rv.task_log_errors(day) if h["task"] == "fair-marks"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(sum(x["n"] for x in hits[0]["lines"]), 2)

    def test_the_logging_doc_covers_the_fair_sinks_so_the_review_does_not_flag_them(self):
        # the review lists a dated sink that IMM_LOGGING.md never names (sec_data "undocumented")
        here = os.path.dirname(os.path.abspath(fm.__file__))
        shutil.copy(os.path.join(here, "IMM_LOGGING.md"), os.path.join(self.root, "IMM_LOGGING.md"))
        u = DAY.isoformat()
        for name in (f"fair_marks_{u}.jsonl", f"fair_results_{u}.jsonl", f"zzz_unnamed_{u}.jsonl"):
            self.write(name, "{}\n")
        self.assertEqual(rv.sec_data(DAY)["undocumented"], ["zzz_unnamed"])
        with open(os.path.join(here, "IMM_LOGGING.md"), encoding="utf-8") as fh:
            doc = fh.read()
        for name in ("fair_raw", "IMM_FAIR_MARKS", "KL imm fair-marks", "fair-marks-task.log"):
            self.assertIn(name, doc)


class ScorecardTests(Base):
    def _mark(self, ts, t, p, bid=None, ask=None, fam="rain_wknd"):
        return {"ts": iso(ts), "family": fam, "ticker": t, "p": p, "bid": bid, "ask": ask}

    def test_settled_markets_score_on_the_last_booked_mark_before_close(self):
        close = et_ts(DAY, 18)
        u = datetime.fromtimestamp(close, timezone.utc).strftime("%Y-%m-%d")
        self.jsonl(f"fair_marks_{u}.jsonl", [
            self._mark(close - 7200, "KXR-A", 0.70, 40, 50),
            self._mark(close - 3600, "KXR-A", 0.80, 45, 55),           # the last with a book
            self._mark(close - 600, "KXR-A", 0.85),                      # no book
            self._mark(close + 600, "KXR-A", 1.0, 99, 100)])             # after the close: never used
        self.jsonl(f"fair_results_{u}.jsonl", [{"ts": iso(close + 900), "ticker": "KXR-A", "family": "rain_wknd",
                                                "result": "yes", "close_ts": iso(close)}])
        self.assertEqual(fs.score_new_results(DAY), 1)
        self.assertEqual(fs.score_new_results(DAY), 0)                  # once
        r = next(rv._iter_jsonl(fs.scores_path()))
        self.assertEqual((r["p"], r["mid"], r["y"], r["event"], r["lead_h"]), (0.8, 50.0, 1, "KXR", 1.0))

    def _scores(self, rows):
        os.makedirs(rv.MOAT_DIR, exist_ok=True)
        with open(fs.scores_path(), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def test_skill_verdicts_with_an_event_bootstrap(self):
        close = iso(et_ts(DAY, 12))
        good = [{"ticker": f"KXG{i}-{j}", "event": f"KXG{i}", "family": "good", "y": y, "close_ts": close,
                 "p": 0.9 if y else 0.1, "mid": 50.0} for i in range(10) for j, y in enumerate((1, 0))]
        bad = [{"ticker": f"KXB{i}-{j}", "event": f"KXB{i}", "family": "bad", "y": y, "close_ts": close,
                "p": 0.1 if y else 0.9, "mid": 50.0} for i in range(10) for j, y in enumerate((1, 0))]
        few = [{"ticker": "KXF1-1", "event": "KXF1", "family": "few", "y": 1, "close_ts": close, "p": 0.9, "mid": 50.0}]
        self._scores(good + bad + few)
        t = fs.settle_table(DAY)
        self.assertEqual(t["good"]["verdict"], "beats the market")
        self.assertGreater(t["good"]["skill_lo"], 0)
        self.assertEqual((t["good"]["events"], t["good"]["disagree_n"]), (10, 20))
        self.assertEqual(t["bad"]["verdict"], "loses to the market")
        self.assertEqual(t["few"]["verdict"], "accumulating")
        self.assertAlmostEqual(t["good"]["brier_model"], 0.01)
        self.assertAlmostEqual(t["good"]["brier_market"], 0.25)

    def test_convergence_and_fills_against_the_model(self):
        s = et_ts(DAY, 9)
        u = datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%d")
        self.jsonl(f"fair_marks_{u}.jsonl", [
            self._mark(s, "KXT-1", 0.60, 38, 42), self._mark(s + 8 * 3600, "KXT-1", 0.60, 48, 52),   # toward
            self._mark(s, "KXT-2", 0.20, 38, 42), self._mark(s + 8 * 3600, "KXT-2", 0.20, 33, 37)])  # toward
        self.jsonl(f"fills_{u}.jsonl", [
            {"ts": int(s + 600), "ticker": "KXT-1", "yes_price_cents": 40, "our_book_side": "bid", "count": 10},
            {"ts": int(s + 900), "ticker": "KXT-1", "yes_price_cents": 50, "our_book_side": "ask", "count": 5}])
        marks = fs.day_marks(DAY)
        c = fs.convergence(DAY, marks)["rain_wknd"]
        self.assertEqual((c["n"], c["toward"]), (2, 1.0))
        self.assertGreater(c["slope"], 0)
        f = fs.fills_edge(DAY, marks)["rain_wknd"]
        # bid at 40 under a 60 fair: +20 x 10; ask at 50 under it: -10 x 5 -> (200 - 50) / 15
        self.assertEqual((f["fills"], f["contracts"], f["edge_c"], f["against_share"]), (2, 15.0, 10.0, 0.333))

    def test_section_render_daily_csv_and_chip(self):
        close = iso(et_ts(DAY, 12))
        self._scores([{"ticker": f"KXB{i}-{j}", "event": f"KXB{i}", "family": "bad", "y": y, "close_ts": close,
                       "p": 0.1 if y else 0.9, "mid": 50.0} for i in range(10) for j, y in enumerate((1, 0))])
        rv._write_json(os.path.join(self.status, "fair_marks_state.json"),
                       {"run": {"at": iso(datetime.now(timezone.utc).timestamp()), "markets": 20, "books": 5,
                                "families": {"bad": 20}, "errors": []}})
        md = fs.sec_models(DAY)
        self.assertEqual(md["counts"]["loses to the market"], 1)
        self.assertEqual(md["health"]["state"], "ok")
        self.assertTrue(os.path.exists(os.path.join(rv.MOAT_DIR, "model_daily.csv")))
        a = []
        rv.render_models(md, a.append)
        text = "\n".join(a)
        self.assertIn("## 13. Model scorecard", text)
        self.assertIn("loses to the market", text)
        self.assertIn(("Models: 1 lose to the market", "amber"), sr.health_checks({"models": md}))
        self.assertIn(("Model log never run", "amber"),
                      sr.health_checks({"models": {"health": {"state": "never run"}, "counts": {}}}))
        fs.sec_models(DAY)                                             # a re-run replaces the day's rows
        self.assertEqual(sum(1 for r in rv._read_rows(os.path.join(rv.MOAT_DIR, "model_daily.csv"))
                             if r["day"] == DAY.isoformat()), 1)


if __name__ == "__main__":
    unittest.main()
