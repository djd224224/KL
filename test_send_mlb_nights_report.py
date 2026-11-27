"""Tests for send_mlb_nights_report.py -- the numbers, on a synthetic
STATUS_DIR (fills, cycle log, guard sink, selection snapshot, run config, bot
state), marks and reward programs handed in. No network: the Kalshi reads are
stubbed in every test."""
import json
import os
import tempfile
import unittest
from collections import Counter
from datetime import date, datetime, timezone
from unittest import mock

import send_mlb_nights_report as mr

HEAD = ("ts,ticker,ext_bid,ext_ask,yes_depth,no_depth,target,est_frac,qual_sides,acct_pos,"
        "own_pos,pool_per_day,quoted,own_bid_ct,own_ask_ct,own_bid_top,own_ask_top,own_pad_bid_ct,"
        "own_pad_ask_ct,want_bid_ct,want_ask_ct,want_pad_ct,hour_mult,rung_lo,room_buy,room_sell,"
        "vol24h,discount,is_scan,is_sticky,reduce_only,fast,run_id,config_hash")
A = "KXMLBNEXTTEAM-26GSPRINGER4-TOR"
B = "KXMLBNEXTTEAM-26MKING34-NYM"
OTHER = "KXGTEMP-27-T1.5"
NEW = "KXNEWONE"
N1 = "KXNEWONE-26NOV28-T1"
N2 = "KXNEWONE-26NOV28-T2"
A2 = "KXMLBNEXTTEAM-26PALONSO25-NYM"
A3 = "KXMLBNEXTTEAM-26PALONSO25-BOS"


def crow(ts, t, bid, ask, frac, pool, bct=60, act=60):
    cols = [ts, t, str(bid), str(ask), "1000", "1000", "1000", str(frac), "2", "0", "0",
            str(pool), "1", str(bct), str(act)] + ["0"] * 17 + ["run1", "cfg1"]
    return ",".join(cols)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def fill(ts, fid, t, series, side, action, n, px, obs):
    return {"ts": ts.timestamp(), "fill_id": fid, "ticker": t, "series": series, "side": side,
            "action": action, "count": float(n), "yes_price_cents": float(px), "is_taker": False,
            "our_book_side": obs}


def cfg_row(ts, run_id, members, enable=True, until="2026-12-03 05:00:00+00:00"):
    c = {"IMM_SCAN_TOP_N": "0"}
    if members is not None:
        c.update({"code:ASTEST_MLB_SERIES": "frozenset({" + ", ".join(f"'{m}'" for m in members) + "})",
                  "code:ASTEST_MLB_ENABLE": enable, "code:ASTEST_MLB_UNTIL": until,
                  "code:ASTEST_MLB_WINDOW_ET": "('00:00', '07:00')"})
    return json.dumps({"ts": ts, "run_id": run_id, "git_sha": "x", "config": c})


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlbn_")
        for p in (mock.patch.object(mr, "STATUS_DIR", self.tmp),
                  mock.patch.object(mr, "OUT_DIR", os.path.join(self.tmp, "mlb_nights")),
                  # never the network: the API reads are stubbed (a test may hand its own in)
                  mock.patch.object(mr, "fetch_marks", lambda ts: {}),
                  mock.patch.object(mr, "fetch_programs", lambda ts, now: {})):
            p.start()
            self.addCleanup(p.stop)

    def write(self, name, lines):
        with open(os.path.join(self.tmp, name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


class TestMlbNightsReport(_Base):
    """The one-series report (no run config: the KXMLBNEXTTEAM fallback)."""
    DAY = date(2026, 11, 27)                     # EST: the night is 05:00-12:00Z

    def setUp(self):
        super().setUp()
        # cycles 30 s apart from 05:00:00Z; a 20-minute outage at 06:00 (capped
        # at 900 s); a cycle after the window (12:30Z) and one before (04:30Z)
        rows = [HEAD]
        stamps = ["2026-11-27T04:30:00Z", "2026-11-27T05:00:00Z", "2026-11-27T05:00:30Z",
                  "2026-11-27T05:01:00Z", "2026-11-27T05:21:00Z", "2026-11-27T12:30:00Z"]
        for s in stamps:
            rows.append(crow(s, OTHER, 40, 44, 0.5, 100.0))          # other series: ts only
            if s in ("2026-11-27T05:00:30Z", "2026-11-27T05:01:00Z", "2026-11-27T05:21:00Z"):
                rows.append(crow(s, A, 10, 14, 0.10, 400.0))
            if s == "2026-11-27T05:01:00Z":
                rows.append(crow(s, B, 40, 50, 0.05, 300.0))
        self.write("cycle_log_2026-11-27.csv", rows)
        fills = [
            # bought 60 YES at 12c on A in the window (05:10Z)
            fill(utc(2026, 11, 27, 5, 10), "f1", A, "KXMLBNEXTTEAM", "yes", "buy", 60, 12, "bid"),
            # the same fill twice (the WS feed and the poll)
            fill(utc(2026, 11, 27, 5, 10), "f1", A, "KXMLBNEXTTEAM", "yes", "buy", 60, 12, "bid"),
            # sold 20 YES at 48c on B (06:30Z)
            fill(utc(2026, 11, 27, 6, 30), "f2", B, "KXMLBNEXTTEAM", "no", "buy", 20, 48, "ask"),
            # after the window and another series: never in the night
            fill(utc(2026, 11, 27, 13, 0), "f3", A, "KXMLBNEXTTEAM", "yes", "buy", 5, 11, "bid"),
            fill(utc(2026, 11, 27, 5, 30), "f4", OTHER, "KXGTEMP", "yes", "buy", 9, 40, "bid")]
        self.write("fills_2026-11-27.jsonl", [json.dumps(f) for f in fills])
        guards = [{"ts": "2026-11-27T05:10:02+00:00", "kind": "enter", "ticker": A,
                   "guard": "astest_fill_pause"},
                  {"ts": "2026-11-27T05:10:02+00:00", "kind": "exit", "ticker": A,
                   "guard": "astest_fill_pause"},
                  {"ts": "2026-11-27T12:01:00+00:00", "kind": "enter", "ticker": A,
                   "guard": "astest_window"}]
        self.write("guard_skips_2026-11-27.jsonl", [json.dumps(g) for g in guards])
        self.marks = {A: {"bid": 20.0, "ask": 30.0, "last": 25.0, "status": "active", "result": ""},
                      B: {"bid": 40.0, "ask": 50.0, "last": 44.0, "status": "active", "result": ""}}
        self.now = utc(2026, 11, 27, 12, 15)                         # 07:15 ET

    def test_the_night_is_00_to_07_et(self):
        self.assertEqual(mr.night_bounds(self.DAY), (utc(2026, 11, 27, 5, 0), utc(2026, 11, 27, 12, 0)))
        self.assertEqual(mr.night_bounds(self.DAY, ("00:00", "24:00")),
                         (utc(2026, 11, 27, 5, 0), utc(2026, 11, 28, 5, 0)))

    def test_rewards_are_the_reconcilers_accrual_for_the_window(self):
        cyc = mr.scan_cycles("KXMLBNEXTTEAM", *mr.night_bounds(self.DAY), today=date(2026, 11, 28))
        # A: 05:00:30 (30 s), 05:01:00 (30 s), 05:21:00 (20 min -> capped 900 s)
        exp_a = 0.10 * 400.0 * (30 + 30 + 900) / 86400.0
        exp_b = 0.05 * 300.0 * 30 / 86400.0
        self.assertAlmostEqual(cyc[A]["est"], exp_a, places=9)
        self.assertAlmostEqual(cyc[B]["est"], exp_b, places=9)
        self.assertEqual(cyc[A]["cycles"], 3)
        self.assertEqual((cyc[A]["first_mid"], cyc[A]["last_mid"]), (12.0, 12.0))
        self.assertNotIn(OTHER, cyc)
        # cached for a finished day: the same answer from the cache, no rescan
        cpath = os.path.join(mr.OUT_DIR, "cycle_cache.json")
        self.assertTrue(os.path.exists(cpath))
        with mock.patch.object(mr, "_scan_day", side_effect=AssertionError("rescanned")):
            again = mr.scan_cycles("KXMLBNEXTTEAM", *mr.night_bounds(self.DAY), today=date(2026, 11, 28))
        self.assertAlmostEqual(again[A]["est"], exp_a, places=9)
        # another series on the same day joins the day's entry; MLB's stays
        oth = mr.scan_cycles("KXGTEMP", *mr.night_bounds(self.DAY), today=date(2026, 11, 28))
        self.assertIn(OTHER, oth)
        with open(cpath, encoding="utf-8") as fh:
            self.assertEqual(set(json.load(fh)["2026-11-27"]["series"]), {"KXMLBNEXTTEAM", "KXGTEMP"})
        # the running day is never cached
        mr.scan_cycles("KXMLBNEXTTEAM", *mr.night_bounds(self.DAY), today=date(2026, 11, 27))
        with open(cpath, encoding="utf-8") as fh:
            self.assertEqual(list(json.load(fh)), ["2026-11-27"])

    def test_trading_is_every_fill_marked_to_the_last_trade(self):
        subject, page, text, ctx = mr.build(self.DAY, self.now, marks=self.marks)
        n = ctx["night"]
        self.assertEqual(len(n["fills"]), 2)                         # f1 once, f2; not f3/f4
        exp = 60 * (25 - 12) / 100.0 + (-1) * 20 * (44 - 48) / 100.0   # +7.80 +0.80
        self.assertAlmostEqual(n["pnl"], exp, places=9)
        self.assertEqual((n["bought"], n["sold"]), (60.0, 20.0))
        self.assertAlmostEqual(n["net"], n["rew"] + n["pnl"], places=9)
        # since start also holds the 13:00Z fill (to the run's now: it is later)
        later = mr.build(self.DAY, utc(2026, 11, 27, 13, 30), marks=self.marks)[3]
        self.assertEqual(len(later["since"]["fills"]), 3)
        pos = {p["ticker"]: p for p in later["since"]["positions"]}
        self.assertEqual(pos[A]["net"], 65.0)
        self.assertAlmostEqual(pos[A]["avg"], (60 * 12 + 5 * 11) / 65.0, places=9)
        self.assertEqual(pos[B]["net"], -20.0)
        # a settled market marks at 100/0
        settled = dict(self.marks, **{A: {"bid": None, "ask": None, "last": 99.0, "status": "finalized",
                                          "result": "yes"}})
        s = mr.build(self.DAY, self.now, marks=settled)[3]["night"]
        self.assertAlmostEqual(s["pnl"], 60 * (100 - 12) / 100.0 + 0.80, places=9)
        self.assertIn("Nights pilot, Fri Nov 27:", subject)
        self.assertIn("2 fills", subject)
        self.assertIn("1 of 1 series quoted", subject)
        self.assertIn("GSPRINGER to TOR", page)
        self.assertEqual(ctx["source"], "no run config found: KXMLBNEXTTEAM only")
        stops = page.split("Stops this night")[1]
        self.assertIn("fill pause (event aside 15 min)", stops)       # the stop that fired
        self.assertNotIn("astest_window", stops)                     # the window itself is no stop
        self.assertTrue(all(ord(c) < 128 for c in subject + page + text))   # the task console is cp1252

    def test_status_stop_file_halt_and_the_end(self):
        self.assertEqual(mr.build(self.DAY, self.now, marks=self.marks)[3]["status"], "ON")
        with open(os.path.join(self.tmp, "imm_state.json"), "w", encoding="utf-8") as fh:
            json.dump({"twk_halt_day": {"ASTEST:MLB": "2026-11-27"}}, fh)
        self.assertIn("HALTED 2026-11-27", mr.build(self.DAY, self.now, marks=self.marks)[3]["status"])
        open(os.path.join(self.tmp, mr.STOP_FILE), "w").close()
        self.assertEqual(mr.build(self.DAY, self.now, marks=self.marks)[3]["status"], "STOPPED (stop file)")
        os.remove(os.path.join(self.tmp, mr.STOP_FILE))
        os.remove(os.path.join(self.tmp, "imm_state.json"))
        end = mr.build(date(2026, 12, 3), utc(2026, 12, 3, 12, 15), marks=self.marks)[3]
        self.assertEqual(end["status"], "ENDED")
        self.assertEqual((end["nights_done"], end["nights_left"]), (6, 0))
        self.assertEqual(end["members_tonight"], [])                 # 12/03's window opens at the end
        first = mr.build(self.DAY, self.now, marks=self.marks)[3]
        self.assertEqual((first["nights_done"], first["nights_left"]), (1, 5))

    def test_an_empty_night_and_one_email_a_day(self):
        subject, page, text, ctx = mr.build(date(2026, 11, 28), utc(2026, 11, 28, 12, 15), marks={})
        self.assertEqual(ctx["night"]["fills"], [])
        self.assertIn("No fills this night.", page)
        self.assertIn("0 fills", subject)
        sent = []
        with mock.patch.object(mr, "send", lambda s, p, t: sent.append(s) or True):
            self.assertEqual(mr.main(["--day", "2026-11-27"]), 0)
            self.assertEqual(mr.main(["--day", "2026-11-27"]), 0)       # the marker: no resend
            self.assertEqual(len(sent), 1)
            self.assertEqual(mr.main(["--day", "2026-11-27", "--force"]), 0)
            self.assertEqual(len(sent), 2)
            self.assertEqual(mr.main(["--day", "2026-11-27", "--dry"]), 0)
            self.assertEqual(len(sent), 2)
            self.assertEqual(mr.main(["--day", "2026-11-27", "--test"]), 0)
            self.assertTrue(sent[-1].startswith("[TEST] "))
            # a check on named series sends but never writes the day's marker
            self.assertEqual(mr.main(["--day", "2026-11-30", "--series", "KXGTEMP"]), 0)
            self.assertFalse(os.path.exists(os.path.join(mr.OUT_DIR, "sent_2026-11-30.marker")))
            self.assertTrue(os.path.exists(os.path.join(mr.OUT_DIR, "report_2026-11-30_KXGTEMP.html")))
        with mock.patch.object(mr, "send", lambda s, p, t: False):
            self.assertEqual(mr.main(["--day", "2026-11-29"]), 1)       # a failed send: rc 1
            self.assertFalse(os.path.exists(os.path.join(mr.OUT_DIR, "sent_2026-11-29.marker")))


class TestNightFamily(_Base):
    """Every series in the family: members from the bot's run config, one
    cycle-log pass for all of them, each series from its own first night."""

    def setUp(self):
        super().setUp()
        # r0 predates the family; r1 (11/26) has MLB; r2 (11/27 19:56Z) adds
        # KXNEWONE; r3 (11/28 13:00Z) switches the family off
        self.write("config_history_2026-11-26.jsonl", [
            cfg_row("2026-11-26T04:45:14+00:00", "r0", None),
            cfg_row("2026-11-26T14:57:24+00:00", "r1", ["KXMLBNEXTTEAM"])])
        self.write("config_history_2026-11-27.jsonl", [
            cfg_row("2026-11-27T19:56:49+00:00", "r2", ["KXMLBNEXTTEAM", NEW])])
        self.write("config_history_2026-11-28.jsonl", [
            cfg_row("2026-11-28T13:00:00+00:00", "r3", ["KXMLBNEXTTEAM", NEW], enable=False)])
        # 11/27: MLB quoted; KXNEWONE quoted too, but before it joined
        self.write("cycle_log_2026-11-27.csv", [
            HEAD, crow("2026-11-27T05:00:00Z", OTHER, 40, 44, 0.5, 100.0),
            crow("2026-11-27T05:00:30Z", A, 10, 14, 0.10, 400.0),
            crow("2026-11-27T05:00:30Z", N1, 30, 40, 0.30, 500.0),
            crow("2026-11-27T05:01:00Z", A, 10, 14, 0.10, 400.0),
            crow("2026-11-27T05:01:30Z", OTHER, 40, 44, 0.5, 100.0)])
        # 11/28: both series in the window
        self.write("cycle_log_2026-11-28.csv", [
            HEAD, crow("2026-11-28T04:59:30Z", OTHER, 40, 44, 0.5, 100.0),
            crow("2026-11-28T05:00:00Z", OTHER, 40, 44, 0.5, 100.0),
            crow("2026-11-28T05:00:30Z", A2, 20, 30, 0.10, 400.0),
            crow("2026-11-28T05:00:30Z", N1, 30, 40, 0.20, 300.0),
            crow("2026-11-28T05:01:00Z", A2, 20, 30, 0.10, 400.0),
            crow("2026-11-28T05:01:00Z", N1, 30, 40, 0.20, 300.0),
            crow("2026-11-28T12:30:00Z", OTHER, 40, 44, 0.5, 100.0)])
        self.write("fills_2026-11-27.jsonl", [
            json.dumps(fill(utc(2026, 11, 27, 5, 10), "m1", A, "KXMLBNEXTTEAM", "yes", "buy", 60, 12, "bid")),
            json.dumps(fill(utc(2026, 11, 27, 6, 0), "x1", N1, NEW, "yes", "buy", 10, 35, "bid"))])
        self.write("fills_2026-11-28.jsonl", [
            json.dumps(fill(utc(2026, 11, 28, 5, 20), "n1", N1, NEW, "no", "buy", 60, 38, "ask"))])
        snap = []
        for ts in ("2026-11-28T05:10:00.100000+00:00", "2026-11-28T05:20:00.100000+00:00"):
            for t, dec in ((N1, "selected"), (N2, "payout_floor"), (A2, "selected"), (A3, "extreme_mid")):
                snap.append(json.dumps({"ts": ts, "ticker": t, "decision": dec, "est_frac": 0.1,
                                        "series": mr.series_of(t)}))
        snap.append(json.dumps({"ts": "2026-11-28T13:00:00.100000+00:00", "ticker": N2, "decision": "one_sided",
                                "series": NEW}))                  # after the window
        self.write("selection_snapshot_2026-11-28.jsonl", snap)
        self.marks = {A: {"bid": 20.0, "ask": 30.0, "last": 25.0, "status": "active", "result": ""},
                      A2: {"bid": 20.0, "ask": 30.0, "last": 25.0, "status": "active", "result": ""},
                      N1: {"bid": 30.0, "ask": 40.0, "last": 34.0, "status": "active", "result": ""}}
        self.programs = {A: (utc(2026, 11, 25, 5).timestamp(), utc(2026, 12, 3, 4, 59).timestamp()),
                         A2: (utc(2026, 11, 25, 5).timestamp(), utc(2026, 12, 3, 4, 59).timestamp()),
                         N1: (utc(2026, 11, 26, 17).timestamp(), utc(2026, 11, 29, 5).timestamp())}
        self.now = utc(2026, 11, 28, 12, 15)

    def test_members_come_from_the_run_config(self):
        fam = mr.family(date(2026, 11, 29), utc(2026, 11, 29, 12, 15))
        self.assertEqual(fam["source"], "the bot's run config")
        self.assertEqual(fam["members"][date(2026, 11, 27)], {"KXMLBNEXTTEAM"})      # r1
        self.assertEqual(fam["members"][date(2026, 11, 28)], {"KXMLBNEXTTEAM", NEW})  # r2
        self.assertEqual(fam["members"][date(2026, 11, 29)], set())                   # r3: off
        self.assertEqual(fam["first"], {"KXMLBNEXTTEAM": date(2026, 11, 27), NEW: date(2026, 11, 28)})
        self.assertEqual(fam["last"][NEW], date(2026, 11, 28))
        self.assertEqual(fam["window"], ("00:00", "07:00"))
        self.assertEqual(fam["until"], utc(2026, 12, 3, 5).timestamp())
        # a run's UNTIL ends membership from the first window opening at it
        runs = [{"ts": utc(2026, 11, 27).timestamp(), "members": frozenset({NEW}),
                 "until": utc(2026, 11, 29, 5).timestamp(), "window": None}]
        self.assertEqual(mr.night_members(runs, date(2026, 11, 28), ("00:00", "07:00")), {NEW})
        self.assertEqual(mr.night_members(runs, date(2026, 11, 29), ("00:00", "07:00")), set())
        # the repr and a comma list both parse
        self.assertEqual(mr._parse_members("frozenset({'KXA', 'KXB'})"), frozenset({"KXA", "KXB"}))
        self.assertEqual(mr._parse_members("frozenset()"), frozenset())
        self.assertEqual(mr._parse_members("KXA, KXB"), frozenset({"KXA", "KXB"}))
        self.assertIsNone(mr._parse_members(None))

    def test_every_series_from_its_own_first_night_in_one_pass(self):
        calls = []
        real = mr._scan_day

        def counted(path, series):
            calls.append((os.path.basename(path), sorted(mr._as_set(series))))
            return real(path, series)

        with mock.patch.object(mr, "_scan_day", counted):
            subject, page, text, ctx = mr.build(date(2026, 11, 28), self.now, marks=self.marks,
                                                programs=self.programs)
        # one scan per day file, every series that needs the day in it
        self.assertEqual(calls, [("cycle_log_2026-11-27.csv", ["KXMLBNEXTTEAM"]),
                                 ("cycle_log_2026-11-28.csv", ["KXMLBNEXTTEAM", NEW])])
        rows = {r["series"]: r for r in ctx["rows"]}
        self.assertEqual(ctx["members_tonight"], ["KXMLBNEXTTEAM", NEW])
        new = rows[NEW]
        self.assertEqual(new["first"], date(2026, 11, 28))
        # since start: only its own nights -- not the 11/27 fill or accrual
        self.assertEqual([f["ticker"] for f in new["since"]["fills"]], [N1])
        self.assertAlmostEqual(new["since"]["rew"], 0.20 * 300.0 * 60 / 86400.0, places=9)
        self.assertAlmostEqual(new["night"]["pnl"], -60 * (34 - 38) / 100.0, places=9)   # sold 38, now 34
        mlb = rows["KXMLBNEXTTEAM"]
        self.assertEqual(len(mlb["since"]["fills"]), 1)              # its 11/27 fill
        self.assertEqual(mlb["night"]["fills"], [])
        self.assertEqual(mlb["nights"], 2)
        # family totals are the series' sums
        self.assertAlmostEqual(ctx["night"]["net"], new["night"]["net"] + mlb["night"]["net"], places=9)
        self.assertAlmostEqual(ctx["since"]["net"], new["since"]["net"] + mlb["since"]["net"], places=9)
        self.assertEqual(ctx["series_quoted"], 2)
        # why markets sat out: the window's decisions only
        self.assertEqual((new["candidates"], dict(new["reasons"])), (2, {"payout_floor": 1}))
        self.assertEqual((mlb["candidates"], dict(mlb["reasons"])), (2, {"extreme_mid": 1}))
        self.assertIn("2 of 2 series quoted", subject)
        self.assertIn("By series", page)
        self.assertIn("from Nov 28", page)
        self.assertIn("sat out: under the $1 floor 1", page)
        self.assertIn(f"  {NEW}: 1 of 2 markets, 1 fills", text)
        self.assertTrue(all(ord(c) < 128 for c in subject + page + text))

    def test_a_series_out_of_the_family_keeps_its_since_start(self):
        ctx = mr.build(date(2026, 11, 29), utc(2026, 11, 29, 12, 15), marks=self.marks,
                       programs=self.programs)[3]
        self.assertEqual(ctx["members_tonight"], [])
        rows = {r["series"]: r for r in ctx["rows"]}
        self.assertFalse(rows[NEW]["member"])
        self.assertEqual(len(rows[NEW]["since"]["fills"]), 1)        # its night of 11/28
        self.assertIn("not in the family tonight", mr.build(
            date(2026, 11, 29), utc(2026, 11, 29, 12, 15), marks=self.marks, programs=self.programs)[1])

    def test_payout_floor_holds_each_market_period_against_one_dollar(self):
        w = ("00:00", "07:00")
        n27, n28 = date(2026, 11, 27), date(2026, 11, 28)
        p_long = (utc(2026, 11, 25, 5).timestamp(), utc(2026, 12, 3, 5).timestamp())
        p1 = (utc(2026, 11, 22, 5).timestamp(), utc(2026, 11, 28, 5).timestamp())   # ends as 11/28 opens
        p2 = (utc(2026, 11, 28, 5).timestamp(), utc(2026, 11, 29, 5).timestamp())
        reads = [(utc(2026, 11, 27, 12, 15).timestamp(), {"M-B": p1}),
                 (utc(2026, 11, 28, 12, 15).timestamp(), {"M-A": p_long, "M-B": p2, "M-C": p_long})]
        by_night = {"M-A": {n27: 0.6, n28: 0.6},          # one period, past $1
                    "M-B": {n27: 0.6, n28: 0.3},          # p1 closed under $1; p2 open, no nights left
                    "M-C": {n28: 0.4},                    # on pace: 4 more nights at 0.4
                    "M-D": {n28: 0.5}}                    # no read covers it
        fv = mr.floor_view(by_night, reads, n28, w, utc(2026, 12, 3, 5).timestamp(), utc(2026, 11, 28, 12, 15))
        self.assertEqual((fv["open"]["mkts"], fv["open"]["past"], fv["open"]["on_pace"]), (3, 1, 1))
        self.assertEqual((fv["closed"]["n"], fv["closed"]["past"]), (1, 0))
        self.assertAlmostEqual(fv["closed"]["under_usd"], 0.6, places=9)
        self.assertAlmostEqual(fv["unknown_usd"], 0.5, places=9)
        self.assertEqual(mr.nights_left_in(p2[1], utc(2026, 12, 3, 5).timestamp(), n28, w), 0)
        self.assertEqual(mr.nights_left_in(p_long[1], utc(2026, 12, 3, 5).timestamp(), n28, w), 4)
        # a night's period comes from the read made after it when one covers it
        self.assertEqual(mr.period_of("M-B", utc(2026, 11, 27, 5).timestamp(), reads), p1)
        cell = mr._floor_cell(fv)
        self.assertIn("1/3 past $1, 1 on pace", cell)
        self.assertIn("closed: 0/1 reached $1", cell)

    def test_program_reads_are_kept_for_later_runs(self):
        self.assertEqual(mr.program_reads(self.now, None), [])
        mr.keep_programs(self.now, self.programs)
        reads = mr.program_reads(utc(2026, 11, 29, 12, 15), {N1: (1.0, 2.0)})
        self.assertEqual(len(reads), 2)
        self.assertEqual(reads[0][1][N1], self.programs[N1])
        self.assertEqual(reads[1][1][N1], (1.0, 2.0))
        # a run that reads them keeps them; a failed read keeps nothing
        with mock.patch.object(mr, "fetch_programs", lambda ts, now: dict(self.programs)):
            mr.build(date(2026, 11, 28), self.now, marks=self.marks)
        with mock.patch.object(mr, "fetch_programs", lambda ts, now: None):
            page = mr.build(date(2026, 11, 28), self.now, marks=self.marks)[1]
        with open(os.path.join(mr.OUT_DIR, "programs_seen.jsonl"), encoding="utf-8") as fh:
            self.assertEqual(len(fh.read().splitlines()), 2)
        self.assertIn("this run's program read failed", page)

    def test_selection_reads_only_the_window(self):
        sel = mr.load_selection({NEW}, *mr.night_bounds(date(2026, 11, 28)))
        self.assertEqual(sel[NEW][N2], Counter({"payout_floor": 2}))
        self.assertNotIn("KXMLBNEXTTEAM", sel)


if __name__ == "__main__":
    unittest.main()
