"""Tests for the 11/13 wave-tool additions (Jack 2026-11-13, the FX + metals wave):
pvd_waves.py datefix (misread_fixes), run --write's fair refresh + datefix calls, and
pvd_wave_tune.py --fair-file. Temp files only, no network. Run with
IMM_STATUS_DIR set to a scratch dir: python -m unittest test_pvd_waves_fair"""
import importlib.util, io, json, os, sys, tempfile, types, unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


pw = _load("pvd_waves.py", "pvd_waves_fairtest")


NOW = datetime(2026, 11, 14, 1, 0, tzinfo=timezone.utc)


def yymondd(ev):
    """The bot's reading (incentive_mm.parse_event_date's YYMONDD), 00:00 UTC is close enough."""
    seg = ev.split("-")[1]
    try:
        return datetime(2000 + int(seg[:2]), pw._MON[seg[2:5]], int(seg[5:7]), tzinfo=timezone.utc)
    except (ValueError, KeyError):
        return None


def mk(close, occ=None, status="active"):
    return {"status": status, "close_time": close, "occurrence_datetime": occ, "expected_expiration_time": close}


class DateFixTests(unittest.TestCase):
    def test_ddmonyy_event_misread_is_fixed_at_its_kalshi_time(self):
        ev = "KXGOLDEVENT-17NOV261100"
        fixes, skip = pw.misread_fixes({ev: ev + "-T388000"}, yymondd,
                                       {ev + "-T388000": mk("2026-11-17T16:00:00Z")}, {}, NOW)
        self.assertEqual(fixes, {ev: "2026-11-17T16:00:00Z"})
        self.assertEqual(skip, [])

    def test_evening_et_event_on_the_next_utc_day(self):
        ev = "KXAUDUSDEVENT-01DEC262030"           # Dec 1 8:30 PM ET = Dec 2 01:30Z
        fixes, _ = pw.misread_fixes({ev: ev + "-T06800"}, yymondd,
                                    {ev + "-T06800": mk("2026-12-02T01:30:00Z")}, {}, NOW)
        self.assertEqual(fixes, {ev: "2026-12-02T01:30:00Z"})

    def test_correctly_read_events_are_left_alone(self):
        ev = "KXUSDJPYEVENT-26NOV261930"           # day 26 = year 26: the bot reads it right
        fixes, skip = pw.misread_fixes({ev: ev + "-T1"}, yymondd, {ev + "-T1": mk("2026-11-27T00:30:00Z")}, {}, NOW)
        self.assertEqual((fixes, skip), ({}, []))
        ev = "KXRAIN-26NOV17"                      # an ordinary YYMONDD ticker, read right
        fixes, skip = pw.misread_fixes({ev: ev + "-T1"}, yymondd, {ev + "-T1": mk("2026-11-18T05:00:00Z")}, {}, NOW)
        self.assertEqual((fixes, skip), ({}, []))

    def test_misread_into_the_future_is_fixed_too(self):
        ev = "KXUSDCADEVENT-30NOV260930"           # bot: 2030-11-26; real: Nov 30 2026 9:30 ET
        fixes, skip = pw.misread_fixes({ev: ev + "-T1"}, yymondd, {ev + "-T1": mk("2026-11-30T14:30:00Z")}, {}, NOW)
        self.assertEqual(fixes, {ev: "2026-11-30T14:30:00Z"})

    def test_existing_entries_are_never_touched(self):
        ev = "KXGOLDEVENT-17NOV261100"
        fixes, _ = pw.misread_fixes({ev: ev + "-T1"}, yymondd, {ev + "-T1": mk("2026-11-17T16:00:00Z")},
                                    {ev: "2026-11-17T15:00:00Z"}, NOW)
        self.assertEqual(fixes, {})

    def test_a_close_far_past_the_event_uses_the_occurrence(self):
        ev = "KXFOO-17NOV26"
        fixes, _ = pw.misread_fixes({ev: ev + "-X"}, yymondd,
                                    {ev + "-X": mk("2027-11-17T16:00:00Z", occ="2026-11-17T20:00:00Z")}, {}, NOW)
        self.assertEqual(fixes, {ev: "2026-11-17T20:00:00Z"})

    def test_a_kalshi_time_off_the_ticker_day_is_skipped(self):
        ev = "KXFOO-17NOV26"
        fixes, skip = pw.misread_fixes({ev: ev + "-X"}, yymondd, {ev + "-X": mk("2027-03-01T16:00:00Z")}, {}, NOW)
        self.assertEqual(fixes, {})
        self.assertIn("check by hand", skip[0][1])          # misread past, Kalshi's day not the ticker's

    def test_closed_or_unread_market_is_skipped(self):
        ev = "KXGOLDEVENT-17NOV261100"
        fixes, skip = pw.misread_fixes({ev: ev + "-T1"}, yymondd,
                                       {ev + "-T1": mk("2026-11-17T16:00:00Z", status="closed")}, {}, NOW)
        self.assertEqual(fixes, {})
        fixes, skip = pw.misread_fixes({ev: ev + "-T1"}, yymondd, {}, {}, NOW)
        self.assertEqual(fixes, {})

    def test_misread_but_not_ddmonyy_is_reported_not_fixed(self):
        ev = "KXBAR-17NOVQ2"                       # whatever this is, not DDMONYY
        fixes, skip = pw.misread_fixes({ev: ev + "-X"}, lambda e: datetime(2017, 11, 1, tzinfo=timezone.utc),
                                       {ev + "-X": mk("2026-11-17T16:00:00Z")}, {}, NOW)
        self.assertEqual(fixes, {})
        self.assertIn("not DDMONYY", skip[0][1])




class FairRunTests(unittest.TestCase):
    def go(self, wave, write=True):
        m = _load("pvd_waves.py", "pvd_waves_fairrun")
        d = tempfile.mkdtemp()
        reg = os.path.join(d, "waves.json")
        json.dump({"waves": {"wX": wave}}, open(reg, "w"))
        m.load = lambda: json.load(open(reg))
        calls = []
        m.run_one = lambda cmd, env=None, tail=4: calls.append([os.path.basename(str(c)) for c in cmd]) or 0
        m.scan_cmd = lambda w, write, group="main": (["scan", group], None)
        m.tune_cmd = lambda name, w, write, group="main": ["tune", group]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = m.cmd_run(types.SimpleNamespace(only=None, write=write))
        return rc, calls, buf.getvalue()

    def base(self, **kw):
        w = {"series": ["KXGOLDEVENT"], "since": "2026-11-13T17:30:00Z", "tune": [], "port": 1, "enabled": True}
        w.update(kw)
        return w

    def test_refresh_runs_between_scan_and_tune(self):
        rc, calls, out = self.go(self.base(fair_cmd=["wave_fair.py", "OUT.json", "--live"]))
        self.assertEqual(rc, 0)
        self.assertEqual(calls[-1][1:3], ["pvd_waves.py", "datefix"])      # then the ticker-date fix
        calls = calls[:-1]
        self.assertEqual([c[0] for c in calls], ["scan", calls[1][0], "tune"])
        self.assertIn("wave_fair.py", calls[1])
        self.assertEqual(calls[1][-2:], ["OUT.json", "--live"])
        self.assertIn("fair refresh", out)

    def test_no_fair_cmd_is_unchanged(self):
        rc, calls, out = self.go(self.base())
        self.assertEqual(calls[:2], [["scan", "main"], ["tune", "main"]])
        self.assertEqual(calls[2][1:4], ["pvd_waves.py", "datefix", "all"])
        self.assertNotIn("fair refresh", out)

    def test_dry_run_does_not_refresh(self):
        rc, calls, out = self.go(self.base(fair_cmd=["wave_fair.py", "OUT.json", "--live"]), write=False)
        self.assertEqual(calls, [["scan", "main"], ["tune", "main"]])
        self.assertIn("dry run: not refreshed", out)

    def test_failed_refresh_does_not_fail_the_pass(self):
        m_calls = []
        rc, calls, out = self.go(self.base(fair_cmd=["wave_fair.py", "OUT.json", "--live"]))
        self.assertEqual(rc, 0)

    def test_near_group_gets_no_refresh(self):
        rc, calls, out = self.go(self.base(series=[], near=["KXFOO"], fair_cmd=["wave_fair.py", "O", "--live"]))
        self.assertEqual(calls[:2], [["scan", "near"], ["tune", "near"]])




def entry(bid_max, ask_min):
    return {"bid_max": bid_max, "ask_min": ask_min, "size": 200, "stop": "2099-01-01T00:00:00Z",
            "period_start": "2026-11-13T18:01:49Z", "follow_book": True}


TOUCH = {  # yes bid, yes ask, last
    "KXUSDJPYEVENT-15NOV261950-T15600": (26.0, 96.0, 93.0),    # fair 100: our ask sold at 93
    "KXUSDMXNEVENT-19NOV261500-T195500": (26.0, 46.0, 0.0),    # fair 0.12: bids 21-29
    "KXGOLDEVENT-17NOV260930-T388000": (47.0, 53.0, 0.0),      # fair 50: the 6c-wide maker
    "KXCOPPEREVENT-17NOV260930-T615000": (44.0, 56.0, 0.0),    # copper: base margin 6c
    "KXSILVEREVENT-17NOV260930-T48000": (30.0, 70.0, 0.0),     # no fair in the file
}
FAIR = {"KXUSDJPYEVENT-15NOV261950-T15600": 100.0, "KXUSDMXNEVENT-19NOV261500-T195500": 0.12,
        "KXGOLDEVENT-17NOV260930-T388000": 50.0, "KXCOPPEREVENT-17NOV260930-T615000": 50.0}


class FairTuneTests(unittest.TestCase):
    def run_tune(self, mod_name, extra, valid_until="2099-01-01T00:00:00Z", fair_text=None):
        mod = _load(mod_name + ".py", "tune_" + mod_name.replace(".", "_"))
        d = tempfile.mkdtemp()
        lst = os.path.join(d, "list.json")
        mk = {"KXUSDJPYEVENT-15NOV261950-T15600": entry(29, 98), "KXUSDMXNEVENT-19NOV261500-T195500": entry(2, 43),
              "KXGOLDEVENT-17NOV260930-T388000": entry(50, 50), "KXCOPPEREVENT-17NOV260930-T615000": entry(47, 53),
              "KXSILVEREVENT-17NOV260930-T48000": entry(33, 67)}
        json.dump({"markets": mk}, open(lst, "w"))
        ff = os.path.join(d, "fair.json")
        open(ff, "w").write(fair_text if fair_text is not None else json.dumps(
            {"fair": FAIR, "margin": {"KXCOPPEREVENT": 6.0}, "valid_until": valid_until}))
        mod.LIST, mod.LOCK = lst, os.path.join(d, "scan.lock")
        mod.read_touches = lambda ts_: ({t: TOUCH[t] for t in ts_ if t in TOUCH}, 0)
        argv = ["pvd_wave_tune.py", "--series", "KXUSDJPYEVENT,KXUSDMXNEVENT,KXGOLDEVENT,KXCOPPEREVENT,KXSILVEREVENT",
                "--since", "2026-11-13T17:30:00Z", "--follow-book", "--reanchor", "3", "--reanchor-step", "5",
                "--reanchor-jump", "10", "--write"] + [x.replace("FAIR", ff) for x in extra]
        old = sys.argv
        sys.argv = argv
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                rc = mod.main()
        finally:
            sys.argv = old
        self.assertEqual(rc, 0, buf.getvalue())
        return json.load(open(lst))["markets"], buf.getvalue()

    def test_clamps_to_fair_with_floors(self):
        L, out = self.run_tune("pvd_wave_tune", ["--fair-file", "FAIR"])
        jpy = L["KXUSDJPYEVENT-15NOV261950-T15600"]
        self.assertEqual(jpy["ask_min"], 98, jpy)              # never sell a ~100c contract under 98
        mxn = L["KXUSDMXNEVENT-19NOV261500-T195500"]
        self.assertEqual(mxn["bid_max"], 2, mxn)               # a ~0c contract: bids <= 2c only
        gold = L["KXGOLDEVENT-17NOV260930-T388000"]
        self.assertEqual((gold["bid_max"], gold["ask_min"]), (47, 53), gold)   # ATM: 3c, joins the 47x53 maker
        cu = L["KXCOPPEREVENT-17NOV260930-T615000"]
        self.assertEqual((cu["bid_max"], cu["ask_min"]), (44, 56), cu)         # copper's own 6c
        ag = L["KXSILVEREVENT-17NOV260930-T48000"]
        self.assertEqual((ag["bid_max"], ag["ask_min"]), (2, 98), ag)          # no fair: floors only
        self.assertIn("fair limits (fair.json", out)
        self.assertIn("1 with no fair", out)
        self.assertIn("fair_clamp", jpy)

    def test_second_run_holds_the_clamp(self):
        # the re-anchor walks the ask limit 5c toward the touch; the clamp brings it back
        L, _ = self.run_tune("pvd_wave_tune", ["--fair-file", "FAIR"])
        self.assertEqual(L["KXUSDJPYEVENT-15NOV261950-T15600"]["ask_min"], 98)

    def test_stale_or_unreadable_file_means_floors_only(self):
        for kw in ({"valid_until": "2000-01-01T00:00:00Z"}, {"fair_text": "{not json"}):
            L, out = self.run_tune("pvd_wave_tune", ["--fair-file", "FAIR"], **kw)
            g = L["KXGOLDEVENT-17NOV260930-T388000"]
            self.assertEqual((g["bid_max"], g["ask_min"]), (2, 98), (kw, g))
            self.assertIn("5 with no fair", out)

    def test_margin_shape(self):
        mod = _load("pvd_wave_tune.py", "tune_shape")
        self.assertAlmostEqual(mod.fair_margin(3.0, 50.0, 0.25), 3.0, places=3)
        self.assertGreater(mod.fair_margin(3.0, 15.87, 0.25), 8.5)     # z = -1: +6c
        self.assertLess(mod.fair_margin(3.0, 0.01, 0.25), 3.5)          # deep tail: ~base




if __name__ == "__main__":
    unittest.main()
