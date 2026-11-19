"""wave_fair.py: the Friday-close reference and the per-source fallback (no network)."""
import importlib.util, json, math, os, sys, tempfile, unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("wave_fair_t", os.path.join(HERE, "wave_fair.py"))
wf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wf)
NOW = datetime(2026, 11, 14, 2, 0, tzinfo=timezone.utc)


def ok(ref=1.0, cur=1.01):
    return lambda sym, starts=None, need_ref=True: (ref if need_ref else None, cur)


def boom(sym, starts=None, need_ref=True):
    raise OSError("down")


class WaveFairTests(unittest.TestCase):
    def test_ref_is_the_median_of_the_four_close_bars(self):
        bars = {t: v for t, v in zip(wf.REF_STARTS, [1.42793, 1.42792, 1.42711, 1.42791])}
        self.assertAlmostEqual(wf._ref(bars), 1.427915)
        bars[wf.T0] = 9.9                                  # a bar after the close is ignored
        self.assertAlmostEqual(wf._ref(bars), 1.427915)

    def test_ref_needs_two_bars(self):
        with self.assertRaises(ValueError):
            wf._ref({wf.REF_STARTS[0]: 1.0})

    def test_every_series_has_a_source(self):
        self.assertEqual(set(wf.PROXY), set(wf.CLOSE))

    def test_all_sources_up(self):
        live, failed = wf.live_moves({}, NOW, {"hl": ok(), "kraken": ok(), "bitso": ok()})
        self.assertEqual(failed, {})
        self.assertEqual(len(live), 9)
        self.assertAlmostEqual(live["KXGOLDEVENT"]["ratio"], 1.01)

    def test_a_failed_source_reuses_a_fresh_previous_price(self):
        prev = {"at": (NOW - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "live": {"KXUSDMXNEVENT": {"src": "bitso:usd_mxn", "ref": 18.54, "now": 18.6, "ratio": 1.003}}}
        live, failed = wf.live_moves(prev, NOW, {"hl": ok(), "kraken": ok(), "bitso": boom})
        self.assertEqual(failed, {})
        self.assertAlmostEqual(live["KXUSDMXNEVENT"]["ratio"], 1.003)
        self.assertIn("from_prev", live["KXUSDMXNEVENT"])

    def test_a_failed_source_with_a_stale_previous_price_gets_no_fair(self):
        prev = {"at": (NOW - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "live": {"KXUSDMXNEVENT": {"ratio": 1.003}}}
        live, failed = wf.live_moves(prev, NOW, {"hl": ok(), "kraken": ok(), "bitso": boom})
        self.assertEqual(set(failed), {"KXUSDMXNEVENT"})
        self.assertNotIn("KXUSDMXNEVENT", live)

    def test_bad_prices_count_as_a_failure(self):
        live, failed = wf.live_moves({}, NOW, {"hl": ok(), "kraken": ok(ref=0.0), "bitso": ok()})
        self.assertEqual(set(failed), {"KXAUDUSDEVENT", "KXUSDCADEVENT"})

    def test_weekend_hours_add_no_trading_time(self):
        sat = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(wf.trading_days(sat), 0.02 + wf.WEEKEND_GAP_DAYS)

    def test_a_later_weekend_counts_from_its_own_close(self):
        t0 = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)
        mon = datetime(2026, 11, 23, 22, 0, tzinfo=timezone.utc)      # Sun 22:00Z open + 24 h
        self.assertAlmostEqual(wf.trading_days(mon, t0), 7 / 5 + wf.WEEKEND_GAP_DAYS)


def U(*a):
    return datetime(*a, tzinfo=timezone.utc)


class WeekendRollTests(unittest.TestCase):
    """2026-11-14 (Jack: "default to running the wave for elections, after economic events, and
    things of similar shapes to what's been run on waves so far"): each weekend's close rolls by
    itself, checked against the 24/7 source's own price at the close."""

    def test_the_shut_window_is_et_and_dst_aware(self):
        self.assertEqual(wf.fx_shut_window(U(2026, 11, 14, 12)), (U(2026, 11, 13, 22), U(2026, 11, 15, 22), True))
        self.assertEqual(wf.fx_shut_window(U(2026, 11, 15, 22)), (U(2026, 11, 13, 22), U(2026, 11, 15, 22), False))
        self.assertEqual(wf.fx_shut_window(U(2026, 11, 20, 21, 59))[2], False)        # Fri 4:59 PM ET
        self.assertEqual(wf.fx_shut_window(U(2026, 11, 20, 22)), (U(2026, 11, 20, 22), U(2026, 11, 22, 22), True))
        self.assertEqual(wf.fx_shut_window(U(2026, 7, 11, 12)), (U(2026, 7, 10, 21), U(2026, 7, 12, 21), True))
        # 2027-03-14 springs forward: Fri 5 PM EST to Sun 5 PM EDT
        self.assertEqual(wf.fx_shut_window(U(2027, 3, 13, 12)), (U(2027, 3, 12, 22), U(2027, 3, 14, 21), True))
        self.assertEqual(wf.fx_next_window(U(2026, 11, 18, 12)), (U(2026, 11, 20, 22), U(2026, 11, 22, 22)))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "closes.json")
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self, vals):
        def f(kind):
            def g(sym, t0):
                self.calls.append(sym)
                v = vals.get(sym)
                if v is None:
                    raise ValueError("not settled yet")
                return v
            return g
        return {"kalshi": f("kalshi"), "yahoo": f("yahoo")}

    def test_the_1113_weekend_keeps_the_closes_entered_by_hand(self):
        close, src, failed = wf.weekend_closes(wf.T0, {}, fetch=self.fetch({}), path=self.path)
        self.assertEqual(close, wf.CLOSE_1113)
        self.assertEqual((failed, self.calls), ({}, []))

    def test_a_new_weekend_fetches_checks_and_keeps_its_closes(self):
        t0 = U(2026, 11, 20, 22)
        vals = {"KXGOLDW": 3900.0, "KXSILVERW": 49.0, "KXCOPPERW": 6.2, "JPY=X": 158.0, "EURUSD=X": 1.10,
                "AUDUSD=X": 0.67, "MXN=X": 18.4, "CAD=X": 1.42, "GBPUSD=X": 1.33}
        ref = {s: vals[sym] * 1.001 for s, (_k, sym) in wf.CLOSE_SRC.items()}     # venues 0.1% away: fine
        ref["KXCOPPEREVENT"] = 6.2 * 1.02                                          # 2% away: refused
        del ref["KXUSDCADEVENT"]                                                   # no venue price: refused
        close, src, failed = wf.weekend_closes(t0, ref, fetch=self.fetch(vals), path=self.path)
        self.assertEqual(set(close), set(wf.CLOSE) - {"KXCOPPEREVENT", "KXUSDCADEVENT"} - set(wf.SESSION_ANCHOR))
        self.assertEqual(close["KXGOLDEVENT"], 3900.0)
        self.assertEqual(src["KXUSDJPYEVENT"], "yahoo:JPY=X")
        self.assertIn("off the 24/7 source", failed["KXCOPPEREVENT"])
        self.assertIn("no 24/7 price", failed["KXUSDCADEVENT"])
        # the next run fetches only the two still missing
        self.calls.clear()
        ref["KXCOPPEREVENT"] = 6.2
        ref["KXUSDCADEVENT"] = 1.42
        close, src, failed = wf.weekend_closes(t0, ref, fetch=self.fetch(vals), path=self.path)
        self.assertEqual(sorted(self.calls), ["CAD=X", "KXCOPPERW"])
        self.assertEqual((set(close), failed), (set(wf.CLOSE) - set(wf.SESSION_ANCHOR), {}))

    def test_metals_before_kalshi_settles_get_no_fair_yet(self):
        t0 = U(2026, 11, 20, 22)
        vals = {"JPY=X": 158.0}
        close, src, failed = wf.weekend_closes(t0, {"KXUSDJPYEVENT": 158.0, "KXGOLDEVENT": 3900.0},
                                               fetch=self.fetch(vals), path=self.path)
        self.assertEqual(set(close), {"KXUSDJPYEVENT"})
        self.assertIn("not settled yet", failed["KXGOLDEVENT"])

    def test_weekday_time_counts_from_now_with_the_gap_only_across_a_weekend(self):
        wed = U(2026, 11, 18, 15)
        self.assertAlmostEqual(wf.trading_days(U(2026, 11, 19, 15), wf.T0, weekday_from=wed), 1.4)
        # Wed 15:00Z -> Fri 22:00Z (2.29 d) + Sun 22:00Z -> Mon 15:00Z (0.71 d), x 7/5, + the gap
        self.assertAlmostEqual(wf.trading_days(U(2026, 11, 23, 15), wf.T0, weekday_from=wed), 3.0 * 1.4 + 0.5)
        # off the hour: steps to whole hours, so the Friday close cuts at 17:00 ET exactly
        self.assertAlmostEqual(wf.trading_days(U(2026, 11, 20, 23), wf.T0, weekday_from=U(2026, 11, 20, 21, 30)),
                               0.5 / 24 * 1.4)          # 21:30-22:00Z only, no gap


class WeekdayFairTests(unittest.TestCase):
    """Jack 2026-11-14: "after-economic events can quote anytime not just when markets are shut,
    but need a fair value": while FX and metals trade the fair prices from now, widens by its
    staleness, stands aside near a strike's measurement and rests nothing without a fair."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (wf.FETCH, wf.CLOSE_FETCH, wf.CLOSES_FILE, wf.ANCHOR_FETCH)
        wf.CLOSES_FILE = os.path.join(self.tmp.name, "closes.json")
        wf.ANCHOR_FETCH = {}                     # no S&P anchor: the FX series alone
        self.need_ref = []

        def venue(sym, starts=None, need_ref=True):
            self.need_ref.append(need_ref)
            s = next(k for k, (_src, v) in wf.PROXY.items() if v == sym)
            return (wf.CLOSE_1113[s] if need_ref else None), wf.CLOSE_1113[s] * 1.001
        wf.FETCH = {"hl": venue, "kraken": venue, "bitso": venue}
        wf.CLOSE_FETCH = {}
        self.out = os.path.join(self.tmp.name, "fair.json")

    def tearDown(self):
        wf.FETCH, wf.CLOSE_FETCH, wf.CLOSES_FILE, wf.ANCHOR_FETCH = self.saved
        self.tmp.cleanup()

    def markets(self, now):
        k = round(wf.CLOSE_1113["KXGOLDEVENT"] * 1.001, 2)
        z = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")     # noqa: E731
        rows = [{"ticker": "KXGOLDEVENT-SOON-T", "floor_strike": k, "strike_type": "greater",
                 "close_time": z(now + timedelta(hours=6))},
                {"ticker": "KXGOLDEVENT-MON-T", "floor_strike": k, "strike_type": "greater",
                 "close_time": z(U(2026, 11, 23, 15))},
                {"ticker": "KXGOLDEVENT-MON-FAR", "floor_strike": round(k * 1.10, 2), "strike_type": "greater",
                 "close_time": z(U(2026, 11, 23, 15))}]
        return lambda s: rows if s == "KXGOLDEVENT" else []

    def run_main(self, now):
        rc = wf.main(self.out, True, now=now, list_markets=self.markets(now))
        self.assertEqual(rc, 0)
        with open(self.out) as fh:
            return json.load(fh)

    def test_a_weekday_fair(self):
        now = U(2026, 11, 18, 15)                                   # Wed 10 AM ET
        doc = self.run_main(now)
        self.assertEqual((doc["mode"], doc["floor"], doc["valid_until"]), ("weekday", 0, "2026-11-18T15:20:00Z"))
        self.assertNotIn("KXGOLDEVENT-SOON-T", doc["fair"])         # 6 h to its measurement: nothing rests
        self.assertAlmostEqual(doc["fair"]["KXGOLDEVENT-MON-T"], 50.0, delta=0.05)
        # 2 sd of 18 min of trading over the sigma to Monday: ~4.9c at the money
        self.assertAlmostEqual(doc["margin_extra"]["KXGOLDEVENT-MON-T"], 4.87, delta=0.1)
        self.assertLess(doc["margin_extra"]["KXGOLDEVENT-MON-FAR"], 0.2)   # 10% out: ~0
        self.assertEqual(doc["close_at"], "2026-11-13T22:00Z")

    def test_the_venues_own_close_is_kept_so_a_weekday_reads_only_the_price_now(self):
        self.run_main(U(2026, 11, 18, 15))
        self.assertTrue(all(self.need_ref))                          # the first run read the close bars
        with open(wf.CLOSES_FILE) as fh:
            book = json.load(fh)
        self.assertEqual(set(book["2026-11-13T22:00Z"]["venue_ref"]), set(wf.CLOSE) - set(wf.SESSION_ANCHOR))
        self.assertEqual(book["2026-11-13T22:00Z"]["close"], wf.CLOSE_1113)
        self.need_ref.clear()
        self.run_main(U(2026, 11, 18, 15, 15))
        self.assertEqual(set(self.need_ref), {False})               # ...the next only the price now

    def test_the_weekend_is_unchanged(self):
        now = U(2026, 11, 14, 12)                                    # Sat
        doc = self.run_main(now)
        self.assertEqual((doc["mode"], doc["floor"], doc["margin_extra"]), ("weekend", 2, {}))
        self.assertEqual(doc["valid_until"], "2026-11-14T12:45:00Z")
        self.assertIn("KXGOLDEVENT-SOON-T", doc["fair"])             # no weekday cutoff on a weekend

    def test_a_weekday_without_live_prices_writes_nothing(self):
        rc = wf.main(self.out, False, now=U(2026, 11, 18, 15), list_markets=self.markets(U(2026, 11, 18, 15)))
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.out))


class SessionAnchorTests(unittest.TestCase):
    """KXINXEVENT (Jack 2026-11-19: "Build a fair, then list"): the S&P 500 strikes' kept close is
    the latest regular-hours ^GSPC 5-min bar and the SP500 perp's bar at the same minute."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "closes.json")
        self.t0 = U(2026, 11, 13, 22)
        # Wed 11/18: bars to the 15:55 ET bar (20:55Z), Yahoo's trailing 16:00 point, then nothing
        self.bars = [(U(2026, 11, 18, 20, 50), 7330.0), (U(2026, 11, 18, 20, 55), 7322.28),
                     (U(2026, 11, 18, 21, 0), 7323.63)]
        self.perp = {U(2026, 11, 18, 20, 50): 7333.0, U(2026, 11, 18, 20, 55): 7326.35}
        self.hl_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fetch(self):
        def hl_bar(coin, start):
            self.hl_calls.append((coin, start))
            if start not in self.perp:
                raise ValueError("no bar")
            return self.perp[start]
        return {"yahoo_bars": lambda sym, now: list(self.bars), "hl_bar": hl_bar}

    def book(self):
        with open(self.path) as fh:
            return json.load(fh)["2026-11-13T22:00Z"]

    def test_the_latest_regular_hours_bar_and_the_perp_at_it(self):
        out, failed = wf.session_anchors(self.t0, U(2026, 11, 19, 3), fetch=self.fetch(), path=self.path)
        self.assertEqual(failed, {})
        self.assertEqual(out["KXINXEVENT"][:2], (7322.28, 7326.35))       # the 15:55 bar, not the 16:00 point
        self.assertEqual(self.hl_calls, [("xyz:SP500", U(2026, 11, 18, 20, 55))])
        wk = self.book()
        self.assertEqual((wk["close"]["KXINXEVENT"], wk["venue_ref"]["KXINXEVENT"]), (7322.28, 7326.35))
        self.assertIn("^GSPC 5m bar 2026-11-18T20:55Z", wk["source"]["KXINXEVENT"])

    def test_a_bar_in_progress_is_not_an_anchor(self):
        now = U(2026, 11, 18, 21, 0, 30)                  # 30 s after the 15:55 bar ended
        out, _f = wf.session_anchors(self.t0, now, fetch=self.fetch(), path=self.path)
        self.assertEqual(out["KXINXEVENT"][:2], (7330.0, 7333.0))

    def test_a_perp_far_off_the_index_is_refused(self):
        self.perp[U(2026, 11, 18, 20, 55)] = 7322.28 * 1.01
        out, failed = wf.session_anchors(self.t0, U(2026, 11, 19, 3), fetch=self.fetch(), path=self.path)
        self.assertEqual(out, {})
        self.assertIn("off the index", failed["KXINXEVENT"])

    def test_a_failed_read_keeps_the_weeks_anchor(self):
        wf.session_anchors(self.t0, U(2026, 11, 19, 3), fetch=self.fetch(), path=self.path)
        out, failed = wf.session_anchors(self.t0, U(2026, 11, 19, 4), fetch={}, path=self.path)
        self.assertEqual((out, failed), ({}, {}))
        self.assertEqual(self.book()["close"]["KXINXEVENT"], 7322.28)

    def test_the_fx_closes_and_the_anchor_share_the_week(self):
        wf.session_anchors(self.t0, U(2026, 11, 19, 3), fetch=self.fetch(), path=self.path)
        close, _src, failed = wf.weekend_closes(wf.T0, {}, fetch={}, path=self.path)
        self.assertEqual(close, dict(wf.CLOSE_1113, KXINXEVENT=7322.28))   # the hand closes still apply
        self.assertEqual(failed, {})

    def test_a_weekday_fair_for_an_at_least_strike(self):
        saved = (wf.FETCH, wf.CLOSE_FETCH, wf.CLOSES_FILE, wf.ANCHOR_FETCH)
        try:
            wf.CLOSES_FILE, wf.ANCHOR_FETCH, wf.CLOSE_FETCH = self.path, self.fetch(), {}
            mids = dict(wf.CLOSE_1113, KXINXEVENT=7326.35 * 1.002)
            wf.FETCH = {k: (lambda sym, starts=None, need_ref=True:
                            ((mids[next(s for s, (_x, v) in wf.PROXY.items() if v == sym)] if need_ref else None),
                             mids[next(s for s, (_x, v) in wf.PROXY.items() if v == sym)]))
                        for k in ("hl", "kraken", "bitso")}
            now = U(2026, 11, 19, 3)
            rows = [{"ticker": "KXINXEVENT-24NOV261015-T735000", "floor_strike": 7350, "strike_type":
                     "greater_or_equal", "close_time": "2026-11-24T15:15:00Z"}]
            out = os.path.join(self.tmp.name, "fair.json")
            self.assertEqual(wf.main(out, True, now=now, list_markets=lambda s: rows if s == "KXINXEVENT" else []), 0)
            with open(out) as fh:
                doc = json.load(fh)
            self.assertAlmostEqual(doc["spot"]["KXINXEVENT"], 7322.28 * 1.002, places=3)
            sig = 0.0125 * math.sqrt(wf.trading_days(U(2026, 11, 24, 15, 15), wf.T0, weekday_from=now))
            self.assertAlmostEqual(doc["fair"]["KXINXEVENT-24NOV261015-T735000"],
                                   100 * wf.N(math.log(7322.28 * 1.002 / 7350) / sig), delta=0.01)
        finally:
            wf.FETCH, wf.CLOSE_FETCH, wf.CLOSES_FILE, wf.ANCHOR_FETCH = saved


if __name__ == "__main__":
    unittest.main()
