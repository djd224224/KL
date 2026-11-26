"""Tests for imm_fair_marks.py (the 15-minute fair log) and imm_fair_scorecard.py
(the daily model scorecard, evidence section 13). A synthetic run-logs tree in a
temp dir; a fake stands in for Kalshi. Nothing reads or writes the live tree."""

import json
import math
import os
import re
import shutil
import tempfile
import types
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
        # never the real bot in a test: imm_quote_gaps would copy the launcher's
        # env into os.environ for every test after this one (kl-test-import-order)
        p = mock.patch.object(fm, "import_bot", side_effect=AssertionError("tests never import the bot"))
        self.import_bot = p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        rv.configure()
        shutil.rmtree(self.root, ignore_errors=True)
        self.assertEqual(self.import_bot.call_count, 0, "a test reached the real bot import")

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
        with mock.patch.object(fm, "kalshi_markets", return_value={}), \
                mock.patch.dict(os.environ, {"IMM_FAIR_MARKS_BOT_VIEW": "0"}):
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
        for name in ("fair_raw", "IMM_FAIR_MARKS", "KL imm fair-marks", "fair-marks-task.log",
                     "IMM_FAIR_MARKS_BOT_VIEW"):
            self.assertIn(name, doc)


class FakeBot:
    """The bot names the BOT VIEW calls, on the bot's own file contracts and
    math (incentive_mm load_ca_fair / ca_fair_p, load_or_fair / or_fair_c,
    load_vercel_fair / vercel_fair_p / vercel_gate_reason, load_hight_fair /
    hight_gate), with globals already set the way a live import leaves them."""
    RAIN_FAIR_SERIES = "KXRAIN"
    _VERCEL_STRIKE_RE = re.compile(r"^T(\d+)(?:[P.](\d+))?$")

    def __init__(self):
        self.CA_FAIR_FILE, self.OR_FAIR_FILE = "orig-ca", "orig-or"
        self.VERCEL_FAIR_FILE, self.HIGHT_FAIR_FILE = "orig-v", "orig-h"
        self._ca_fair_state = {"mtime": 0.0, "entries": {"KEEP": {}}, "moved_at": {"KEEP": 1.0}}
        self._or_fair_state = {"mtime": 0.0, "entries": {}, "moved_at": {}}
        self._vercel_state = {"mtime": 0.0, "entries": {}, "moved_at": {}}
        self._hight_state = {"mtime": 0.0, "events": {}}

    @staticmethod
    def _ts(s):
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

    @staticmethod
    def series_of(t):
        return t.split("-")[0]

    def _read(self, attr):
        with open(getattr(self, attr), encoding="utf-8") as fh:
            return json.load(fh)

    def load_ca_fair(self):
        self._ca_fair_state["entries"] = {s: {"mu": e["mu"], "sigma": e["sigma"], "ts": self._ts(e["fetched_at"])}
                                          for s, e in self._read("CA_FAIR_FILE")["entries"].items()}

    def ca_fair_p(self, t, now):
        e = self._ca_fair_state["entries"].get(self.series_of(t))
        if e is None or now - e["ts"] > 3600:
            return None
        return 0.5 * math.erfc((float(t.rsplit("-T", 1)[1]) - e["mu"]) / (e["sigma"] * math.sqrt(2.0)))

    def load_or_fair(self):
        self._or_fair_state["entries"] = {
            ev: {"mu": e["mu"], "sigma": e["sigma"], "complete": e["complete"], "ts": self._ts(e["fetched_at"])}
            for ev, e in self._read("OR_FAIR_FILE")["entries"].items()}

    def or_fair_c(self, t, now):
        e = self._or_fair_state["entries"].get(t.rsplit("-", 1)[0])
        if e is None or e["complete"]:
            return None
        k = float(t.rsplit("-T", 1)[1])
        return 50.0 * math.erfc((k - e["mu"]) / (max(1e-9, e["sigma"]) * math.sqrt(2.0)))

    def load_vercel_fair(self):
        self._vercel_state["entries"] = {ev: {"x_l": e["x_l"], "errs": e["errs"], "ts": self._ts(e["fetched_at"])}
                                         for ev, e in self._read("VERCEL_FAIR_FILE")["entries"].items()}

    def vercel_fair_p(self, e, k):
        thr = math.floor(k * 10 + 1e-9) / 10.0 + 0.1 - 0.05
        hit = sum(1 for x in e["errs"] if e["x_l"] + x >= thr - 1e-9)
        return min(max(hit / len(e["errs"]), 0.01), 0.99)

    def vercel_gate_reason(self, t, now, ext_bid, ext_ask):
        ev, _, ks = t.rpartition("-")
        e, m = self._vercel_state["entries"].get(ev), self._VERCEL_STRIKE_RE.match(ks)
        if e is None or m is None:
            return "no Vercel read", {"reason": "no_read"}
        p = self.vercel_fair_p(e, float(f"{m.group(1)}.{m.group(2) or 0}"))
        return ("decided", {"reason": "decided", "fair": p * 100}) if p < 0.03 or p > 0.97 else ("", {})

    def load_hight_fair(self):
        self._hight_state["events"] = {ev: dict(e, ts=self._ts(e["fetched_at"]))
                                       for ev, e in self._read("HIGHT_FAIR_FILE")["events"].items()}

    def hight_gate(self, t, now, ext_bid, ext_ask):
        e = self._hight_state["events"].get(t.rsplit("-", 1)[0])
        if e is None:
            return "no read", {"reason": "no_read"}
        if e.get("skip"):
            return "no fair: " + e["skip"], {"reason": "skip"}
        p = (e.get("markets") or {}).get(t)
        return ("", {"fair": round(p * 100, 2)}) if p is not None else ("not in the read", {})


class BotViewTests(Base):
    def snapshot(self, now, groups):
        """selection_snapshot rows: [(ts, [tickers]), ...] (the bot's hourly candidate dump)."""
        u = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        rows = [{"ts": datetime.fromtimestamp(ts, timezone.utc).isoformat(), "ticker": t, "decision": "quote",
                 "pad": "x" * 200} for ts, ts_list in groups for t in ts_list]
        return self.jsonl(f"selection_snapshot_{u}.jsonl", rows)

    def files(self, now):
        u = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        f = iso(now - 60)
        self.write("carbon_arc_fair.json", json.dumps({"generated_at": iso(now), "entries": {
            "KXAMZNCC": {"mu": 100.0, "sigma": 2.0, "month": "2026-11", "fetched_at": f}}}))
        self.write("openrouter_fair.json", json.dumps({"generated_at": iso(now), "entries": {
            "KXTOKENUSE-26NOV30": {"mu": 250.0, "sigma": 10.0, "complete": False, "fetched_at": f},
            "KXTOKENUSE-26NOV23": {"mu": 231.0, "sigma": 0.0, "complete": True, "fetched_at": f}}}))
        self.write("vercel_fair.json", json.dumps({"at": iso(now), "entries": {
            "KXOPENVREQ-28NOV26": {"x_l": 10.0, "errs": [-1.0, -0.5, 0.0, 0.5, 1.0], "fetched_at": f}}}))
        self.write("hight_fair.json", json.dumps({"generated_at": iso(now), "events": {
            "KXHIGHNY-26NOV25": {"fetched_at": f, "skip": None,
                                 "markets": {"KXHIGHNY-26NOV25-B50.5": 0.3, "KXHIGHNY-26NOV25-T55": 0.1}},
            "KXHIGHCHI-26NOV25": {"fetched_at": f, "skip": "a midnight high",
                                  "markets": {"KXHIGHCHI-26NOV25-B40.5": 0.5}}}}))
        # standard shape with nothing priced right now; a count file with no fair
        self.write("ercot_fair.json", json.dumps({"generated_at": iso(now), "markets": {},
                                                  "events": {"E": {"markets": {"E-T1": {"type": "greater"}}}}}))
        self.write("pokemon_fair.json", json.dumps({"fetched_at": iso(now), "markets": {
            "KXPOKEMON-26NOV-X-1": {"err": "unmapped event"}}}))
        self.write("datacenter_fair.json", json.dumps({"updated_at": iso(now), "states": {"TX": {"count": 3}}}))
        self.write(f"cycle_log_{u}.csv", f"ts,ticker,ext_bid,ext_ask\n{iso(now - 120)},KXAMZNCC-26DEC05-T100,48,52\n")
        self.snapshot(now, [(now - 3600, ["KXAMZNCC-26DEC05-T90"]),
                            (now - 300, ["KXAMZNCC-26DEC05-T100", "KXAMZNCC-26DEC05-T104",
                                         "KXTOKENUSE-26NOV30-T240", "KXTOKENUSE-26NOV23-T200",
                                         "KXOPENVREQ-28NOV26-T10P2", "KXOPENVREQ-28NOV26-T5",
                                         "KXNOTAFAIR-26DEC01-T3"])])

    def test_each_family_is_priced_by_the_bots_own_functions_and_its_globals_put_back(self):
        now = et_ts(DAY, 15)
        self.files(now)
        bot, calls = FakeBot(), []

        def api(tickers):
            calls.append(sorted(tickers))
            return {t: {"bid": 40.0, "ask": 45.0, "status": "active", "result": "", "close_ts": None,
                        "type": "binary"} for t in tickers}

        o = fm.run(now=now, markets_fn=api, bot=lambda: bot)
        self.assertEqual(o["errors"], [])
        fam = o["families"]
        self.assertEqual((fam["carbon_arc"], fam["openrouter"], fam["vercel"], fam["hight"]), (2, 1, 1, 2))
        self.assertEqual((fam["ercot"], fam["pokemon"]), (0, 0))       # standard, nothing priced: not raw
        self.assertNotIn("datacenter", fam)
        self.assertEqual(sorted(o["raw_files"]), ["carbon_arc", "datacenter", "hight", "openrouter", "vercel"])
        u = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        marks = {m["ticker"]: m for m in self.lines(f"fair_marks_{u}.jsonl")}
        self.assertEqual(set(marks), {"KXAMZNCC-26DEC05-T100", "KXAMZNCC-26DEC05-T104", "KXTOKENUSE-26NOV30-T240",
                                      "KXOPENVREQ-28NOV26-T10P2", "KXHIGHNY-26NOV25-B50.5", "KXHIGHNY-26NOV25-T55"})
        t100 = marks["KXAMZNCC-26DEC05-T100"]
        self.assertEqual((t100["p"], t100["bid"], t100["book_src"], t100["family"]), (0.5, 48.0, "cycle", "carbon_arc"))
        self.assertEqual(t100["src_ts"], fm._iso(now - 60))           # the model's own read time
        self.assertAlmostEqual(marks["KXAMZNCC-26DEC05-T104"]["p"], 0.02275, places=5)   # P(N > 2 sd)
        self.assertAlmostEqual(marks["KXTOKENUSE-26NOV30-T240"]["p"], 0.84134, places=5)  # cents -> probability
        self.assertEqual(marks["KXOPENVREQ-28NOV26-T10P2"]["p"], 0.4)  # 2 of 5 errors clear 10.25
        self.assertEqual((marks["KXHIGHNY-26NOV25-B50.5"]["p"], marks["KXHIGHNY-26NOV25-B50.5"]["book_src"]),
                         (0.3, "api"))                                 # the file's own market, not a candidate
        # top of book read for every bot-view market without a cycle-log book, in one call
        self.assertEqual(calls, [sorted(set(marks) - {"KXAMZNCC-26DEC05-T100"})])
        # the bot module is left as it was found
        self.assertEqual((bot.CA_FAIR_FILE, bot.OR_FAIR_FILE, bot.VERCEL_FAIR_FILE, bot.HIGHT_FAIR_FILE),
                         ("orig-ca", "orig-or", "orig-v", "orig-h"))
        self.assertEqual(bot._ca_fair_state, {"mtime": 0.0, "entries": {"KEEP": {}}, "moved_at": {"KEEP": 1.0}})
        self.assertEqual(bot._hight_state["events"], {})
        # the CLI hands run() the bot import
        with mock.patch.object(fm, "import_bot", return_value=FakeBot()) as imp, \
                mock.patch.object(fm, "kalshi_markets", side_effect=api):
            self.assertEqual(fm.main(["run", "--dry"]), 0)
        self.assertEqual(imp.call_count, 1)

    def test_the_universe_is_the_newest_snapshot_even_when_the_tail_cuts_into_it(self):
        now = et_ts(DAY, 15)
        newest = [f"KXB-26DEC01-T{i}" for i in range(300)]
        self.snapshot(now, [(now - 3600, [f"KXA-26DEC01-T{i}" for i in range(50)]),
                            (now - 120, newest[:150]), (now - 100, newest[150:])])   # one dump over 20 s
        self.assertEqual(fm.universe_tickers(), set(newest))
        with mock.patch.object(fm, "UNIVERSE_TAIL_BYTES", 2000):            # a tail inside the newest dump
            self.assertEqual(fm.universe_tickers(), set(newest))
        # today's file just started (no rows yet): yesterday's newest dump
        u2 = datetime.fromtimestamp(now + 86400, timezone.utc).strftime("%Y-%m-%d")
        self.write(f"selection_snapshot_{u2}.jsonl", "")
        self.assertEqual(fm.universe_tickers(), set(newest))
        for n in os.listdir(self.status):
            if n.startswith("selection_snapshot_"):
                os.remove(os.path.join(self.status, n))
        self.assertEqual(fm.universe_tickers(), set())

    def test_failures_are_failed_errors_and_the_standard_marks_still_land(self):
        now = et_ts(DAY, 15)
        self.files(now)
        self.write("approval_fair.json", json.dumps({"generated_at": iso(now), "markets": {"KXAPR-1": {"p": 0.4}}}))
        nothing = lambda t: {}  # noqa: E731
        o = fm.run(now=now, markets_fn=nothing, bot=mock.Mock(side_effect=ImportError("no bot here")))
        self.assertEqual(o["errors"], ["bot view FAILED: ImportError: no bot here"])
        self.assertEqual(o["families"]["approval"], 1)
        self.assertNotIn("carbon_arc", o["families"])
        bot = FakeBot()
        bot.load_ca_fair = mock.Mock(side_effect=ValueError("bad file"))
        o = fm.run(now=now + 60, markets_fn=nothing, bot=lambda: bot)
        self.assertIn("bot view carbon_arc FAILED: ValueError: bad file", o["errors"])
        self.assertEqual(o["families"]["vercel"], 1)                     # the others still priced
        self.assertEqual((bot.CA_FAIR_FILE, bot._ca_fair_state["entries"]), ("orig-ca", {"KEEP": {}}))
        o = fm.run(now=now + 120, markets_fn=nothing, bot=lambda: types.SimpleNamespace())
        self.assertIn("bot view vercel FAILED: AttributeError", " | ".join(o["errors"]))
        self.assertTrue(all(rv.TASK_LOG_ERR.search(e) for e in o["errors"]))   # the review reports each
        loud = FakeBot()
        real_load = loud.load_vercel_fair

        def warn():
            print("2026-11-24 20:00:00Z [IMM] ! vercel fair file unreadable: half written")
            real_load()
        loud.load_vercel_fair = warn
        o = fm.run(now=now + 180, markets_fn=nothing, bot=lambda: loud)
        self.assertIn("bot view vercel: ! vercel fair file unreadable: half written", o["errors"])
        gate = mock.Mock(side_effect=AssertionError("not called"))
        with mock.patch.dict(os.environ, {"IMM_FAIR_MARKS_BOT_VIEW": "0"}):
            o = fm.run(now=now + 240, markets_fn=nothing, bot=gate)
        self.assertEqual((gate.call_count, o["errors"]), (0, []))


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
