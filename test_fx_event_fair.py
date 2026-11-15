"""fx_event_fair.py: the live FX + metals "after economic events" fair (Jack 2026-11-14: "after-
economic events can quote anytime not just when markets are shut, but need a fair value" ->
"Yes, build it"). No network: the Kalshi reads and the 24/7 sources are stand-ins, the kept
closes a temp file."""
import importlib.util
import json
import math
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import fx_event_fair as fx

HERE = os.path.dirname(os.path.abspath(__file__))


def U(*a):
    return datetime(*a, tzinfo=timezone.utc)


CLOSE = {"KXUSDJPYEVENT": 159.686, "KXEURUSDEVENT": 1.1028, "KXAUDUSDEVENT": 0.6774,
         "KXUSDMXNEVENT": 18.523, "KXUSDCADEVENT": 1.4267, "KXGBPUSDEVENT": 1.3209,
         "KXGOLDEVENT": 3884.10, "KXSILVEREVENT": 48.75, "KXCOPPEREVENT": 6.15}


class Constants(unittest.TestCase):
    def test_the_model_is_wave_fairs(self):
        spec = importlib.util.spec_from_file_location("wf_c", os.path.join(HERE, "wave_fair.py"))
        wf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wf)
        self.assertEqual(wf.VOL_D, fx.VOL_D)
        self.assertEqual(wf.PROXY, fx.PROXY)
        self.assertEqual(set(wf.MARGIN), set(fx.MARGIN))
        self.assertEqual(set(wf.CLOSE), set(fx.SERIES))
        self.assertEqual(wf.WEEKEND_GAP_DAYS, fx.WEEKEND_GAP_DAYS)
        for k, v in (("WEEKDAY_CUTOFF_H", fx.CUTOFF_H), ("WEEKEND_FLOOR", fx.WEEKEND_FLOOR),
                     ("WEEKDAY_FLOOR", fx.WEEKDAY_FLOOR)):
            if hasattr(wf, k):                   # wave_fair's weekday mode (11/14)
                self.assertEqual(getattr(wf, k), v, k)
        if hasattr(wf, "WEEKDAY_CUTOFF_H"):      # that version carries copper's 2c too (11/14 evening)
            self.assertEqual(wf.MARGIN, fx.MARGIN)


class Calendar(unittest.TestCase):
    def test_the_shut_window_is_et_and_dst_aware(self):
        self.assertEqual(fx.fx_shut_window(U(2026, 11, 14, 12)), (U(2026, 11, 13, 22), U(2026, 11, 15, 22), True))
        self.assertEqual(fx.fx_shut_window(U(2026, 11, 15, 22))[2], False)
        self.assertEqual(fx.fx_shut_window(U(2026, 11, 20, 21, 59))[2], False)
        self.assertEqual(fx.fx_shut_window(U(2026, 7, 11, 12)), (U(2026, 7, 10, 21), U(2026, 7, 12, 21), True))
        self.assertEqual(fx.fx_shut_window(U(2027, 3, 13, 12)), (U(2027, 3, 12, 22), U(2027, 3, 14, 21), True))

    def test_trading_days(self):
        t0 = U(2026, 11, 13, 22)
        # the weekend: from the close, the gap always
        self.assertAlmostEqual(fx.trading_days(U(2026, 11, 16, 22), t0), 1.4 + 0.5)
        # a weekday: from now, the gap only across a weekend
        wed = U(2026, 11, 18, 15)
        self.assertAlmostEqual(fx.trading_days(U(2026, 11, 19, 15), t0, weekday_from=wed), 1.4)
        self.assertAlmostEqual(fx.trading_days(U(2026, 11, 23, 15), t0, weekday_from=wed), 3.0 * 1.4 + 0.5)

    def test_the_margin(self):
        # at the money: the 2c base (every series, copper too since 11/14), + on a weekday the requote term
        p, m, b, a = fx.strike_fair("KXGOLDEVENT", 100.0, 100.0, 2.0, False)
        self.assertAlmostEqual(p, 0.5)
        self.assertAlmostEqual(m, 2.0)
        self.assertEqual((b, a), (48, 52))
        p, m, b, a = fx.strike_fair("KXGOLDEVENT", 100.0, 100.0, 2.0, True)
        rq = 100 * fx._phi(0) * fx.REQUOTE_SD * math.sqrt(fx.REQUOTE_MIN / 1440 * 7 / 5) / math.sqrt(2.0)
        self.assertAlmostEqual(m, 2.0 + rq)
        self.assertAlmostEqual(fx.strike_fair("KXCOPPEREVENT", 100.0, 100.0, 2.0, False)[1], 2.0)
        self.assertEqual(set(fx.MARGIN.values()), {2.0})      # Jack 11/14: "make copper and all metals/fx at the 2c margin"
        # a deep tail: floors 2c on the weekend, 0 on a weekday
        self.assertEqual(fx.strike_fair("KXGOLDEVENT", 100.0, 140.0, 2.0, False)[2:], (2, 2))
        self.assertEqual(fx.strike_fair("KXGOLDEVENT", 100.0, 140.0, 2.0, True)[2:], (0, 2))
        self.assertEqual(fx.strike_fair("KXGOLDEVENT", 140.0, 100.0, 2.0, False)[2:], (98, 98))
        self.assertEqual(fx.strike_fair("KXGOLDEVENT", 140.0, 100.0, 2.0, True)[2:], (98, 100))


def market(t, k, close):
    return {"ticker": t, "event_ticker": t.rsplit("-", 1)[0], "strike_type": "greater", "floor_strike": k,
            "close_time": close.strftime("%Y-%m-%dT%H:%M:%SZ")}


class Watch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.closes = os.path.join(self.tmp.name, "closes.json")
        self.write_closes()
        self.mids = {s: CLOSE[s] * 1.001 for s in CLOSE}        # each source 0.1% over its close
        self.fail = {}
        self.mk = [market("KXGOLDEVENT-23NOV261500-T388000", 3888.0, U(2026, 11, 23, 15)),
                   market("KXGOLDEVENT-18NOV262000-T388000", 3888.0, U(2026, 11, 18, 20)),
                   market("KXUSDJPYEVENT-23NOV261500-T15990", 159.9, U(2026, 11, 23, 15)),
                   dict(market("KXGOLDEVENT-23NOV261500-B388000", 3888.0, U(2026, 11, 23, 15)),
                        strike_type="between")]

    def tearDown(self):
        self.tmp.cleanup()

    def write_closes(self, close=None, refs=None, key="2026-11-13T22:00Z"):
        with open(self.closes, "w") as fh:
            json.dump({key: {"close": close if close is not None else CLOSE,
                             "venue_ref": refs if refs is not None else CLOSE}}, fh)

    def watch(self):
        def get_json(path, params):
            s = params["series_ticker"]
            return {"markets": [m for m in self.mk if m["ticker"].startswith(s + "-")]}
        return fx.FxEventWatch(get_json, closes_path=self.closes,
                               fetch=lambda: (dict(self.mids), dict(self.fail)))

    def test_a_weekend_read(self):
        snap = self.watch().refresh(U(2026, 11, 14, 12).timestamp())
        self.assertEqual((snap["mode"], snap["close_at"], snap["model"]["floor"]), ("weekend", "2026-11-13T22:00Z", 2))
        g = snap["series"]["KXGOLDEVENT"]
        self.assertAlmostEqual(g["spot"], 3884.10 * 1.001)       # the close x the source's move since it
        e = snap["markets"]["KXGOLDEVENT-23NOV261500-T388000"]
        sig = 0.0113 * math.sqrt(fx.trading_days(U(2026, 11, 23, 15), U(2026, 11, 13, 22)))
        p = 0.5 * (1 + math.erf(math.log(3884.10 * 1.001 / 3888.0) / sig / math.sqrt(2)))
        self.assertAlmostEqual(e["p"], p, places=4)
        self.assertLess(e["margin"], 2.1)                         # near the money: ~the 2c base
        self.assertEqual((e["bid_max"], e["ask_min"]), (int(math.floor(100 * p - e["margin"])),
                                                        int(math.ceil(100 * p + e["margin"]))))
        self.assertIn("p", snap["markets"]["KXGOLDEVENT-18NOV262000-T388000"])   # no cutoff on a weekend
        self.assertIn("not an 'above' strike", snap["markets"]["KXGOLDEVENT-23NOV261500-B388000"]["err"])

    def test_a_weekday_read_cuts_near_measurement_and_widens(self):
        now = U(2026, 11, 18, 15)                                 # Wed 10 AM ET
        snap = self.watch().refresh(now.timestamp())
        self.assertEqual((snap["mode"], snap["model"]["floor"]), ("weekday", 0))
        near = snap["markets"]["KXGOLDEVENT-18NOV262000-T388000"]   # 5 h to its measurement
        self.assertTrue(near.get("cut"))
        self.assertIn("within 12 h", near["err"])
        far = snap["markets"]["KXGOLDEVENT-23NOV261500-T388000"]
        wk = self.watch().refresh(U(2026, 11, 14, 12).timestamp())["markets"]["KXGOLDEVENT-23NOV261500-T388000"]
        self.assertGreater(far["margin"], wk["margin"] + 1.0)      # the requote term

    def test_no_close_kept_stands_the_series_aside(self):
        self.write_closes(close={s: v for s, v in CLOSE.items() if s != "KXGOLDEVENT"})
        snap = self.watch().refresh(U(2026, 11, 14, 12).timestamp())
        self.assertIn("no close kept", snap["markets"]["KXGOLDEVENT-23NOV261500-T388000"]["err"])
        self.assertIn("p", snap["markets"]["KXUSDJPYEVENT-23NOV261500-T15990"])
        # another weekend's closes only: nothing for this one
        self.write_closes(key="2026-11-06T22:00Z")
        snap = self.watch().refresh(U(2026, 11, 14, 12).timestamp())
        self.assertTrue(all("err" in e for e in snap["markets"].values()))

    def test_the_kept_closes_reload_when_their_file_changes(self):
        w = self.watch()
        self.write_closes(close={}, refs={})
        self.assertIn("err", w.refresh(U(2026, 11, 14, 12).timestamp())["markets"]["KXGOLDEVENT-23NOV261500-T388000"])
        time.sleep(0.02)
        self.write_closes()
        os.utime(self.closes, (time.time() + 5, time.time() + 5))
        self.assertIn("p", w.refresh(U(2026, 11, 14, 12, 0, 15).timestamp())["markets"]["KXGOLDEVENT-23NOV261500-T388000"])

    def test_a_failed_source_keeps_its_last_price_and_ages(self):
        w = self.watch()
        t1 = U(2026, 11, 14, 12).timestamp()
        w.refresh(t1)
        self.mids.pop("KXUSDJPYEVENT")
        self.fail["KXUSDJPYEVENT"] = "hl: URLError"
        snap = w.refresh(t1 + 60)
        e = snap["markets"]["KXUSDJPYEVENT-23NOV261500-T15990"]
        self.assertEqual(e["spot_ts"], t1)                         # the gate's TTL ages it out
        self.assertTrue(any("hl: URLError" in x for x in snap["errors"]))
        fresh = self.watch()
        self.assertIn("err", fresh.refresh(t1)["markets"]["KXUSDJPYEVENT-23NOV261500-T15990"])   # never read

    def test_a_jump_holds_the_series(self):
        w = self.watch()
        t1 = U(2026, 11, 14, 12).timestamp()
        w.refresh(t1)
        self.mids["KXGOLDEVENT"] *= 1 + 0.36 * fx.VOL_D["KXGOLDEVENT"]   # 0.4% in a minute
        snap = w.refresh(t1 + 60)
        self.assertAlmostEqual(snap["series"]["KXGOLDEVENT"]["hold_until"], t1 + 60 + fx.JUMP_HOLD_SECS)
        self.assertIsNone(snap["series"]["KXUSDJPYEVENT"]["hold_until"])
        self.mids["KXUSDJPYEVENT"] *= 1 + 0.3 * fx.VOL_D["KXUSDJPYEVENT"]  # under the bar: no hold
        self.assertIsNone(w.refresh(t1 + 75)["series"]["KXUSDJPYEVENT"]["hold_until"])


class Mids(unittest.TestCase):
    def test_one_call_a_source(self):
        calls = []

        def get(url, body=None):
            calls.append(url)
            if "hyperliquid" in url:
                names = ["xyz:GOLD", "xyz:SILVER", "xyz:COPPER", "xyz:JPY", "xyz:EUR", "xyz:GBP", "xyz:TSLA"]
                return [{"universe": [{"name": n} for n in names]},
                        [{"midPx": str(i + 1.5)} for i in range(len(names))]]
            if "kraken" in url:
                return {"error": [], "result": {"AUDUSD": {"b": ["0.6745"], "a": ["0.6747"]},
                                                "ZUSDZCAD": {"b": ["1.4305"], "a": ["1.4307"]}}}
            return {"payload": {"bid": "18.695", "ask": "18.705"}}
        mids, err = fx.fetch_mids(get)
        self.assertEqual(err, {})
        self.assertEqual(len(calls), 3)
        self.assertAlmostEqual(mids["KXGOLDEVENT"], 1.5)
        self.assertAlmostEqual(mids["KXUSDCADEVENT"], 1.4306)
        self.assertAlmostEqual(mids["KXAUDUSDEVENT"], 0.6746)
        self.assertAlmostEqual(mids["KXUSDMXNEVENT"], 18.70)

    def test_a_wide_book_is_not_a_price(self):
        # Bitso's weekend USD/MXN: the spread flickered 0.005% <-> 0.29% and the mid with it
        def get(url, body=None):
            if "hyperliquid" in url:
                return [{"universe": [{"name": "xyz:GOLD"}]}, [{"midPx": "3800"}]]
            if "kraken" in url:
                return {"error": [], "result": {"AUDUSD": {"b": ["0.6743"], "a": ["0.6749"]},
                                                "ZUSDZCAD": {"b": ["1.4300"], "a": ["1.4330"]}}}
            return {"payload": {"bid": "18.6760", "ask": "18.7230"}}
        mids, err = fx.fetch_mids(get)
        self.assertNotIn("KXUSDMXNEVENT", mids)
        self.assertIn("0.25% wide", err["KXUSDMXNEVENT"])
        self.assertNotIn("KXUSDCADEVENT", mids)                  # 0.21% wide
        self.assertAlmostEqual(mids["KXAUDUSDEVENT"], 0.6746)     # 0.09%: taken

    def test_a_wide_book_keeps_the_last_good_price(self):
        w = Watch("test_a_weekend_read")
        w.setUp()
        try:
            watch = w.watch()
            t1 = U(2026, 11, 14, 12).timestamp()
            watch.refresh(t1)
            w.mids.pop("KXUSDMXNEVENT")
            w.fail["KXUSDMXNEVENT"] = "bitso:usd_mxn book 0.25% wide"
            snap = watch.refresh(t1 + 30)
            self.assertEqual(snap["series"]["KXUSDMXNEVENT"]["mid_ts"], t1)
            self.assertIsNone(snap["series"]["KXUSDMXNEVENT"]["hold_until"])
        finally:
            w.tearDown()

    def test_a_source_down_fails_only_its_series(self):
        def get(url, body=None):
            if "kraken" in url:
                raise OSError("down")
            if "hyperliquid" in url:
                return [{"universe": [{"name": "xyz:GOLD"}]}, [{"midPx": "3800"}]]
            return {"payload": {"bid": "18.695", "ask": "18.705"}}
        mids, err = fx.fetch_mids(get)
        self.assertEqual(set(err), {"KXAUDUSDEVENT", "KXUSDCADEVENT", "KXSILVEREVENT", "KXCOPPEREVENT",
                                    "KXUSDJPYEVENT", "KXEURUSDEVENT", "KXGBPUSDEVENT"})
        self.assertEqual(set(mids), {"KXGOLDEVENT", "KXUSDMXNEVENT"})


if __name__ == "__main__":
    unittest.main()
