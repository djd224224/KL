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


if __name__ == "__main__":
    unittest.main()
