"""Tests for openrouter_fair.py (OpenRouter daily token totals -> fair values
for incentive_mm's KXTOKENUSE / KXTOKENUSEM gate, 2026-09-27)."""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import openrouter_fair as orf

T = 1e12


def flat_daily(end: date, n: int, per_day_t: float = 20.0):
    return {end - timedelta(days=i): per_day_t * T for i in range(n)}


class TestWindows(unittest.TestCase):

    def test_parse_weekly_monthly_and_cross_month(self):
        self.assertEqual(
            orf.parse_window("If the OpenRouter total token usage for Sep 21–27, "
                             "2026 is above 164T tokens", 2026),
            (date(2026, 9, 21), date(2026, 9, 27)))
        self.assertEqual(
            orf.parse_window("usage for September 2026 (measured August 31 - "
                             "September 27) is above 800T", 2026),
            (date(2026, 8, 31), date(2026, 9, 27)))
        self.assertEqual(
            orf.parse_window("usage for Sep 28–Oct 4, 2026 is above", 2026),
            (date(2026, 9, 28), date(2026, 10, 4)))
        self.assertEqual(
            orf.parse_window("(measured December 28 - January 24)", 2027),
            (date(2026, 12, 28), date(2027, 1, 24)))
        self.assertIsNone(orf.parse_window("observed at 10:00 AM ET on Sep 28, 2026", 2026))
        self.assertIsNone(orf.parse_window("", 2026))

    def test_parse_the_october_month_wording(self):
        # 2026-09-30: KXTOKENUSEM-26OCT26 says "October 2026 (Sep 28–Oct 25)"
        # -- no "measured", no year inside the range -- and got no window, so
        # every selected strike stood aside "no OpenRouter read"
        self.assertEqual(
            orf.parse_window("If the OpenRouter AI token usage for October 2026 "
                             "(Sep 28–Oct 25) is above 850T tokens, then the "
                             "market resolves to Yes.", 2026),
            (date(2026, 9, 28), date(2026, 10, 25)))
        self.assertEqual(
            orf.parse_window("This event will resolve based on the value for the UTC "
                             "calendar days from October 2026 (Sep 28–Oct 25), as "
                             "observed at 10:00 AM ET on Oct 26, 2026.", 2026),
            (date(2026, 9, 28), date(2026, 10, 25)))
        self.assertEqual(                                         # a year wrap
            orf.parse_window("usage for January 2027 (Dec 28–Jan 24) is above", 2027),
            (date(2026, 12, 28), date(2027, 1, 24)))
        self.assertEqual(orf.parse_window("week (Oct 5-11) total", 2026),
                         (date(2026, 10, 5), date(2026, 10, 11)))
        self.assertIsNone(orf.parse_window("as observed (Oct 26) at 10:00 AM ET", 2026))

    def test_windows_from_markets_with_weekly_fallback(self):
        mk = [
            {"event_ticker": "KXTOKENUSE-26SEP28", "close_time": "2026-09-28T03:59:00Z",
             "rules_primary": "for Sep 21–27, 2026 is above 164T"},
            {"event_ticker": "KXTOKENUSE-26OCT05", "close_time": "2026-10-05T03:59:00Z",
             "rules_primary": "no dates here"},
            {"event_ticker": "KXTOKENUSEM-26SEP28", "close_time": "2026-09-28T03:59:00Z",
             "rules_primary": "(measured August 31 - September 27)"},
            {"event_ticker": "KXTOKENUSEM-26OCT26", "close_time": "2026-10-26T03:59:00Z",
             "rules_primary": "no dates here"},
        ]
        w = orf.windows_from_markets(mk)
        self.assertEqual(w["KXTOKENUSE-26SEP28"]["start"], "2026-09-21")
        self.assertEqual((w["KXTOKENUSE-26OCT05"]["start"], w["KXTOKENUSE-26OCT05"]["end"]),
                         ("2026-09-28", "2026-10-04"))           # ticker-date fallback
        self.assertEqual(w["KXTOKENUSEM-26SEP28"]["end"], "2026-09-27")
        self.assertNotIn("KXTOKENUSEM-26OCT26", w)               # no monthly fallback
        unparsed = []
        w = orf.windows_from_markets(mk, unparsed)
        self.assertEqual(unparsed, ["KXTOKENUSEM-26OCT26"])      # named, not dropped
        self.assertNotIn("KXTOKENUSE-26OCT05", unparsed)         # the fallback counts


class TestModel(unittest.TestCase):

    def setUp(self):
        self.today = date(2026, 9, 27)                           # a Sunday
        self.daily = flat_daily(self.today - timedelta(days=1), 40)

    def test_flat_history_week_with_one_day_left(self):
        with mock.patch.object(orf, "OR_TREND_WEIGHT", 0.5):
            f = orf.window_fair(self.daily, date(2026, 9, 21), date(2026, 9, 27), self.today)
        # six known days of 20T, one remaining day ~20T (flat: no trend, weekday
        # factor 1), plus the measured one-day bias
        self.assertEqual((f["known"], f["days"], f["complete"]), (6, 7, False))
        self.assertAlmostEqual(f["known_sum"], 120.0)
        self.assertAlmostEqual(f["mu"], 120.0 + 20.0 + orf.OR_BIAS_WEIGHT * orf.BIAS[1] * 20.0, places=3)
        self.assertAlmostEqual(f["sigma"], orf.OR_SIGMA_MULT * orf.RMSE[1] * 20.0, places=3)

    def test_complete_window_has_zero_sigma(self):
        f = orf.window_fair(self.daily, date(2026, 9, 14), date(2026, 9, 20), self.today)
        self.assertTrue(f["complete"])
        self.assertEqual(f["sigma"], 0.0)
        self.assertAlmostEqual(f["mu"], 140.0)
        self.assertEqual(orf.p_above(139.9, f["mu"], f["sigma"]), 1.0)
        self.assertEqual(orf.p_above(140.1, f["mu"], f["sigma"]), 0.0)

    def test_needs_two_weeks_of_history(self):
        thin = flat_daily(self.today - timedelta(days=1), 10)
        self.assertIsNone(orf.window_fair(thin, date(2026, 9, 21), date(2026, 9, 27), self.today))

    def test_a_spike_day_is_capped_in_the_run_rate_not_the_known_days(self):
        # 2026-10-06: a launch day (+50% on its prior-7 median) counts in full
        # toward the window it sits in, but the run rate reads it at 1 +
        # OR_SPIKE_CAP (1.15) x that median
        spike = dict(self.daily)
        sp = self.today - timedelta(days=2)                      # 9/25, known
        spike[sp] = 30.0 * T
        with mock.patch.object(orf, "OR_SPIKE_CAP", 0.15):
            run = orf.cap_spikes(spike)
            self.assertAlmostEqual(run[sp] / T, 23.0)
            self.assertEqual(run[sp - timedelta(days=1)], spike[sp - timedelta(days=1)])
            first = min(spike)                                    # no 7 days before it
            self.assertEqual(run[first], spike[first])
            f = orf.window_fair(spike, date(2026, 9, 21), date(2026, 9, 27), self.today)
        self.assertAlmostEqual(f["known_sum"], 130.0)             # actual, uncapped
        self.assertEqual(f["spike_capped"], ["2026-09-25"])
        base = (6 * 20.0 + 23.0) / 7
        g = (base / 20.0 - 1.0) * orf.OR_TREND_WEIGHT
        want = 130.0 + base * (1 + g) ** (4 / 7) + orf.OR_BIAS_WEIGHT * orf.BIAS[1] * base
        self.assertAlmostEqual(f["base"], round(base, 4), places=4)
        self.assertAlmostEqual(f["mu"], want, places=3)
        # off: the old model, the spike in the base
        with mock.patch.object(orf, "OR_SPIKE_CAP", 0.0):
            g0 = orf.window_fair(spike, date(2026, 9, 21), date(2026, 9, 27), self.today)
            self.assertEqual(orf.cap_spikes(spike), spike)
        self.assertAlmostEqual(g0["base"], round((6 * 20.0 + 30.0) / 7, 4), places=4)
        self.assertEqual(g0["spike_capped"], [])
        self.assertGreater(g0["mu"], f["mu"])

    def test_growth_raises_the_remaining_days(self):
        grow = {}
        for i in range(40):
            d = self.today - timedelta(days=1 + i)
            grow[d] = (20.0 if i < 7 else 18.0) * T        # last week up ~11%
        flat = orf.window_fair(self.daily, date(2026, 9, 21), date(2026, 9, 27), self.today)
        up = orf.window_fair(grow, date(2026, 9, 21), date(2026, 9, 27), self.today)
        self.assertGreater(up["mu"] - up["known_sum"], flat["mu"] - flat["known_sum"])


class TestWriteAndKey(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="orf_test_")
        self.out = os.path.join(self.tmp, "openrouter_fair.json")
        self.vint = os.path.join(self.tmp, "vint.jsonl")
        self.now = datetime(2026, 9, 27, 5, 0, tzinfo=timezone.utc)
        self.daily = flat_daily(date(2026, 9, 26), 40)
        self.windows = {"KXTOKENUSE-26SEP28": {"series": "KXTOKENUSE",
                                               "start": "2026-09-21", "end": "2026-09-27"},
                        "KXTOKENUSE-26OCT05": {"series": "KXTOKENUSE",
                                               "start": "2026-09-28", "end": "2026-10-04"}}

    def test_write_file_and_one_vintage_per_new_day(self):
        ok, miss = orf.write_fair_file(self.out, daily=self.daily, windows=self.windows,
                                       now=self.now, vintage_path=self.vint)
        self.assertEqual((ok, miss), (2, 0))
        d = json.load(open(self.out, encoding="utf-8"))
        e = d["entries"]["KXTOKENUSE-26SEP28"]
        self.assertEqual((e["known"], e["complete"], e["series"]), (6, False, "KXTOKENUSE"))
        self.assertEqual(d["entries"]["KXTOKENUSE-26OCT05"]["known"], 0)
        self.assertEqual(d["last_day"], "2026-09-26")
        orf.write_fair_file(self.out, daily=self.daily, windows=self.windows,
                            now=self.now, vintage_path=self.vint)
        with open(self.vint, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 1)                 # same day: no new line
        self.daily[date(2026, 9, 27)] = 19.0 * T
        orf.write_fair_file(self.out, daily=self.daily, windows=self.windows,
                            now=self.now + timedelta(days=1), vintage_path=self.vint)
        with open(self.vint, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)

    def test_fetch_windows_retries_a_rate_limit(self):
        busy = mock.Mock(status_code=429)
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"markets": [
            {"event_ticker": "KXTOKENUSE-26SEP28", "close_time": "2026-09-28T03:59:00Z",
             "rules_primary": "for Sep 21\u201327, 2026 is above 164T"}]}
        ok.raise_for_status.return_value = None
        with mock.patch.object(orf.requests, "get", side_effect=[busy, ok]) as g, \
                mock.patch.object(orf.time, "sleep") as sl:
            w = orf.fetch_windows(series=("KXTOKENUSE",))
        self.assertEqual(g.call_count, 2)
        sl.assert_called_once()
        self.assertEqual(w["KXTOKENUSE-26SEP28"]["start"], "2026-09-21")

    def test_failed_window_read_reuses_cached_windows(self):
        orf.write_fair_file(self.out, daily=self.daily, windows=self.windows,
                            now=self.now, vintage_path=self.vint)
        with mock.patch.object(orf, "fetch_windows", side_effect=RuntimeError("429")):
            ok, miss = orf.write_fair_file(self.out, daily=self.daily, now=self.now,
                                           vintage_path=self.vint)
        self.assertEqual((ok, miss), (2, 0))
        os.remove(self.out)                              # no cache: the error surfaces
        with mock.patch.object(orf, "fetch_windows", side_effect=RuntimeError("429")):
            with self.assertRaises(RuntimeError):
                orf.write_fair_file(self.out, daily=self.daily, now=self.now,
                                    vintage_path=self.vint)

    def test_no_key_writes_nothing(self):
        with mock.patch.object(orf, "api_key", return_value=""):
            self.assertEqual(orf.write_fair_file(self.out, windows=self.windows), (0, 0))
        self.assertFalse(os.path.exists(self.out))

    def test_api_key_sources(self):
        cfg = os.path.join(self.tmp, "k.json")
        with open(cfg, "wb") as f:                                   # Notepad BOM
            f.write(b"\xef\xbb\xbf" + json.dumps({"key": "sk-or-v1-abc"}).encode())
        with mock.patch.object(orf, "OR_KEY_FILE", cfg), \
                mock.patch.dict(os.environ, {"IMM_OR_API_KEY": ""}):
            self.assertEqual(orf.api_key(), "sk-or-v1-abc")
        bare = os.path.join(self.tmp, "k.txt")
        with open(bare, "w", encoding="utf-8") as f:
            f.write("my key: sk-or-v1-xyz\n")
        with mock.patch.object(orf, "OR_KEY_FILE", bare), \
                mock.patch.dict(os.environ, {"IMM_OR_API_KEY": ""}):
            self.assertEqual(orf.api_key(), "sk-or-v1-xyz")
        with mock.patch.dict(os.environ, {"IMM_OR_API_KEY": "sk-or-env"}):
            self.assertEqual(orf.api_key(), "sk-or-env")

    def test_fetch_daily_sums_rows_with_bearer(self):
        resp = mock.Mock()
        resp.json.return_value = {"data": [
            {"date": "2026-09-25", "model_permaslug": "a", "total_tokens": "1000000000000"},
            {"date": "2026-09-25", "model_permaslug": "other", "total_tokens": "500000000000"},
            {"date": "2026-09-26", "model_permaslug": "a", "total_tokens": "2000000000000"},
            {"date": "bad"}]}
        with mock.patch.object(orf.requests, "get", return_value=resp) as g:
            d = orf.fetch_daily("sk-or-x", date(2026, 9, 25), date(2026, 9, 26))
        self.assertEqual(d, {date(2026, 9, 25): 1.5 * T, date(2026, 9, 26): 2.0 * T})
        self.assertEqual(g.call_args.kwargs["headers"]["Authorization"], "Bearer sk-or-x")


class TestSignedReads(unittest.TestCase):
    """incentive_mm's signed Kalshi reader (2026-09-27): the window read goes
    through it first, the public endpoint is only the fallback. No network."""

    PAGE = {"markets": [
        {"event_ticker": "KXTOKENUSE-26SEP28", "close_time": "2026-09-28T03:59:00Z",
         "rules_primary": "for Sep 21-27, 2026 is above 164T"}]}

    def setUp(self):
        p = mock.patch.object(orf, "_signed_err", None)
        p.start()
        self.addCleanup(p.stop)

    def public_ok(self):
        ok = mock.Mock(status_code=200)
        ok.json.return_value = self.PAGE
        ok.raise_for_status.return_value = None
        return ok

    def test_signed_read_first_and_no_public_call(self):
        seen = []

        def get_json(path, params):
            seen.append((path, params))
            return self.PAGE

        with mock.patch.object(orf.requests, "get",
                               side_effect=AssertionError("public read")):
            w = orf.fetch_windows(series=("KXTOKENUSE",), get_json=get_json)
        self.assertEqual(seen, [("/markets", {"series_ticker": "KXTOKENUSE",
                                              "status": "open", "limit": 200})])
        self.assertEqual(w["KXTOKENUSE-26SEP28"]["start"], "2026-09-21")

    def test_signed_failure_falls_back_and_each_error_is_logged_once(self):
        boom = mock.Mock(side_effect=RuntimeError("HttpError(429 Too Many Requests)"))
        with mock.patch.object(orf.requests, "get", return_value=self.public_ok()) as g, \
                mock.patch.object(orf, "_log") as lg:
            w = orf.fetch_windows(series=("KXTOKENUSE", "KXTOKENUSEM"), get_json=boom)
            self.assertEqual(g.call_count, 2)                # both read publicly
            self.assertEqual(lg.call_count, 1)               # the repeat is silent
            self.assertIn("signed Kalshi read failed", lg.call_args[0][0])
            orf.fetch_windows(series=("KXTOKENUSE",), get_json=lambda p, q: self.PAGE)
            self.assertEqual(g.call_count, 2)
            self.assertEqual(lg.call_count, 2)
            self.assertIn("signed Kalshi reads back", lg.call_args[0][0])
        self.assertEqual(w["KXTOKENUSE-26SEP28"]["end"], "2026-09-27")

    def test_a_non_dict_signed_read_counts_as_a_failure(self):
        with mock.patch.object(orf.requests, "get", return_value=self.public_ok()) as g, \
                mock.patch.object(orf, "_log"):
            w = orf.fetch_windows(series=("KXTOKENUSE",), get_json=lambda p, q: None)
        g.assert_called_once()
        self.assertIn("KXTOKENUSE-26SEP28", w)

    def test_write_fair_file_hands_the_reader_to_the_window_read(self):
        tmp = tempfile.mkdtemp(prefix="orf_signed_")
        reader = mock.Mock()
        with mock.patch.object(orf, "fetch_windows", return_value={}) as fw:
            orf.write_fair_file(os.path.join(tmp, "f.json"),
                                daily=flat_daily(date(2026, 9, 26), 40),
                                now=datetime(2026, 9, 27, 5, 0, tzinfo=timezone.utc),
                                vintage_path=os.path.join(tmp, "v.jsonl"),
                                get_json=reader)
        fw.assert_called_once()
        self.assertIs(fw.call_args.kwargs["get_json"], reader)

    def test_october_month_gets_an_entry_and_an_unparsed_event_is_missing(self):
        tmp = tempfile.mkdtemp(prefix="orf_month_")
        month = [{"event_ticker": "KXTOKENUSEM-26OCT26", "close_time": "2026-10-26T03:59:00Z",
                  "title": "Will OpenRouter AI token usage for October 2026 (Sep 28–Oct 25) "
                           "be above 850T tokens?",
                  "rules_primary": "If the OpenRouter AI token usage for October 2026 "
                                   "(Sep 28–Oct 25) is above 850T tokens, then the market "
                                   "resolves to Yes."},
                 {"event_ticker": "KXTOKENUSEM-26NOV23", "close_time": "2026-11-23T04:59:00Z",
                  "rules_primary": "no dates here"}]
        reader = lambda path, params: {"markets": month if params["series_ticker"] == "KXTOKENUSEM" else []}
        ok, miss = orf.write_fair_file(os.path.join(tmp, "f.json"),
                                       daily=flat_daily(date(2026, 9, 30), 40),
                                       now=datetime(2026, 10, 1, 2, 0, tzinfo=timezone.utc),
                                       vintage_path=os.path.join(tmp, "v.jsonl"),
                                       get_json=reader)
        self.assertEqual((ok, miss), (1, 1))
        with open(os.path.join(tmp, "f.json"), encoding="utf-8") as f:
            d = json.load(f)
        e = d["entries"]["KXTOKENUSEM-26OCT26"]
        self.assertEqual((e["start"], e["end"], e["known"], e["days"]),
                         ("2026-09-28", "2026-10-25", 3, 28))
        self.assertEqual(d["missing"], ["KXTOKENUSEM-26NOV23"])


if __name__ == "__main__":
    unittest.main()
