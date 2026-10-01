"""Tests for openrouter_share_fair.py (OpenRouter's Market Share chart ->
fair values for incentive_mm's KX<AUTHOR>SHARE gate, 2026-09-30)."""

import json
import math
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

import openrouter_share_fair as osf

# the chart's marketShareData for three settled weeks, read 2026-10-01
SEP07 = {"openai": 1308813616, "deepseek": 1254459626, "google": 1031073759,
         "tencent": 400352869, "z-ai": 386281278, "qwen": 289737616,
         "anthropic": 140780376, "mistralai": 126306241, "meta-llama": 122440770,
         "Others": 487389749}
SEP14 = {"deepseek": 1366842265, "google": 1001575468, "openai": 917492311,
         "z-ai": 504811099, "qwen": 361489530, "tencent": 345888264,
         "anthropic": 145511385, "mistralai": 140777889, "xiaomi": 103391019,
         "Others": 503007794}
SEP21 = {"deepseek": 1332850606, "google": 1120528475, "openai": 976132128,
         "z-ai": 490905598, "qwen": 348530903, "stealth": 170877249,
         "anthropic": 149857862, "xiaomi": 136355984, "mistralai": 133609877,
         "Others": 603994280}


def week(x, ys):
    return {"x": x, "ys": dict(ys)}


def flat_history(end: date, n: int, shares: dict, total=1e9):
    """n complete weeks ending with the week of `end`, constant shares."""
    return [week((end - timedelta(days=7 * i)).isoformat(),
                 {k: v / 100.0 * total for k, v in shares.items()})
            for i in range(n - 1, -1, -1)]


def lb_rows(day: date, counts: dict, text=True):
    return [{"date": f"{day.isoformat()} 00:00:00",
             "model_permaslug": f"{a}/model-{i}", "count": c}
            for i, (a, c) in enumerate(counts.items())]


class TestChartReproducesSettlement(unittest.TestCase):

    def test_settled_values_to_the_rounding(self):
        # Kalshi's settled values: week of Sep 21 (the -26SEP28 events, and
        # the rankings page's own summary table), Sep 14 (-26SEP21), Sep 7
        # (-26SEP14). Rounded half-up to one decimal, as published.
        sh = dict(osf.chart_shares([week("2026-09-07", SEP07), week("2026-09-14", SEP14),
                                    week("2026-09-21", SEP21)]))

        def r1(x):
            return math.floor(x * 10 + 0.5) / 10
        for a, v in {"deepseek": 24.4, "google": 20.5, "openai": 17.9, "z-ai": 9.0,
                     "qwen": 6.4, "stealth": 3.1, "anthropic": 2.7, "xiaomi": 2.5,
                     "mistralai": 2.4, "Others": 11.1}.items():
            self.assertEqual(r1(sh["2026-09-21"][a]), v, a)
        for a, v in {"deepseek": 25.4, "google": 18.6, "z-ai": 9.4, "qwen": 6.7,
                     "tencent": 6.4, "anthropic": 2.7}.items():
            self.assertEqual(r1(sh["2026-09-14"][a]), v, a)
        for a, v in {"openai": 23.6, "deepseek": 22.6, "google": 18.6,
                     "anthropic": 2.5, "qwen": 5.2}.items():
            self.assertEqual(r1(sh["2026-09-07"][a]), v, a)
        # nine named authors + Others, every week
        for ys in (SEP07, SEP14, SEP21):
            self.assertEqual(len([k for k in ys if k != osf.OTHERS]), osf.SHARE_NAMED)

    def test_p_yes_uses_the_rounding_edge(self):
        # "above 3.1%" on a value rounded to 0.1 -> YES from 3.15
        self.assertAlmostEqual(osf.p_yes(3.1, 3.15, 0.1, 1.0), 0.5)
        self.assertAlmostEqual(osf.p_yes(3.1, 3.15, 0.1, 0.8), 0.4)
        self.assertEqual(osf.p_yes(3.1, 3.14, 0.0, 1.0), 0.0)
        self.assertEqual(osf.p_yes(3.1, 3.15, 0.0, 0.9), 0.9)


class TestParsing(unittest.TestCase):

    def test_week_from_the_ticker_then_rules(self):
        rules = ("If Anthropic scores above 3.1% on OpenRouter text market share by "
                 "model author week of Sep 28, 2026, then the market resolves to Yes.")
        self.assertEqual(osf.week_of("KXANTHSHARE-26OCT05", rules), date(2026, 9, 28))
        self.assertEqual(osf.week_of("KXANTHSHARE-26OCT05", ""), date(2026, 9, 28))
        self.assertEqual(osf.week_of("KXANTHSHARE-27JAN04", ""), date(2026, 12, 28))
        # the August events' rules named the wrong week; the ticker settled
        self.assertEqual(osf.week_of("KXOPENSHARE-26AUG24", "week of Aug 10, 2026"),
                         date(2026, 8, 17))
        self.assertEqual(osf.week_of("KXANTHSHARE-ANTH", rules), date(2026, 9, 28))
        self.assertIsNone(osf.week_of("KXANTHSHARE-ANTH", ""))

    def test_author_counts_text_only(self):
        rows = [{"model_permaslug": "google/gemini-x", "count": 70},
                {"model_permaslug": "google/embed-y", "count": 1000},     # not text
                {"model_permaslug": "deepseek/v4:free", "count": 30},
                {"model_permaslug": "unknown/new-model", "count": 5},    # counted
                {"model_permaslug": "", "count": 9}, {"model_permaslug": "x/y", "count": None}]
        cat = {"google/gemini-x": True, "google/embed-y": False, "deepseek/v4": True}
        by, tot = osf.author_counts(rows, cat)
        self.assertEqual(by, {"google": 70.0, "deepseek": 30.0, "unknown": 5.0})
        self.assertEqual(tot, 105.0)
        self.assertEqual(osf.rows_date([{"date": "2026-09-30 00:00:00"}]), date(2026, 9, 30))

    def test_vol_winsorized_and_current_week_left_out(self):
        weeks = []
        base = date(2026, 6, 1)
        vals = [10, 10.5, 10, 10.5, 10, 10.5, 10, 16.5, 10, 10.5]      # one +6 jump
        for i, v in enumerate(vals):
            weeks.append(week((base + timedelta(days=7 * i)).isoformat(),
                              {"a": v, "Others": 100 - v}))
        cur = base + timedelta(days=7 * (len(vals) - 1))
        vol = osf.author_vol(weeks, cur + timedelta(days=2))          # last week running
        # the running week (10.5) is left out: eight changes, one jump each way
        ch = [0.5, -0.5, 0.5, -0.5, 0.5, -0.5, 6.5, -6.5]
        cap = max(0.3, 3 * sorted(abs(x) for x in ch)[len(ch) // 2])
        want = math.sqrt(sum(min(cap, abs(x)) ** 2 for x in ch) / len(ch))
        self.assertAlmostEqual(vol["a"], want, places=6)
        self.assertLess(vol["a"], math.sqrt(sum(x * x for x in ch) / len(ch)))
        self.assertNotIn("Others", vol)
        self.assertNotIn("b", osf.author_vol([week("2026-06-01", {"b": 1, "Others": 9}),
                                              week("2026-06-08", {"b": 2, "Others": 8})],
                                             date(2026, 7, 1)))       # < 4 changes


class TestModel(unittest.TestCase):
    W = date(2026, 9, 28)

    def _hist(self, cur=None):
        h = flat_history(self.W - timedelta(days=7), 10,
                         {"a": 20.0, "b": 10.0, "Others": 70.0})
        return h + ([week(self.W.isoformat(), cur)] if cur else [])

    def test_coverage_days_and_ratio(self):
        self.assertEqual(osf.coverage(self.W, 3e9, date(2026, 9, 30), 1e9), (3.0, "days"))
        e, how = osf.coverage(self.W, 2e9, date(2026, 9, 30), 1e9)
        self.assertEqual(e, 3.0)
        self.assertIn("ratio says 2.00", how)
        self.assertEqual(osf.coverage(self.W, 2e9, None, 1e9), (2.0, "ratio"))
        self.assertEqual(osf.coverage(self.W, 2e9, None, 0.0), (0.0, "none"))

    def test_data_current_needs_both_feeds_on_yesterday(self):
        now = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)
        today0 = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
        self.assertEqual(osf.data_current(now, date(2026, 9, 30), today0 + 60), (True, ""))
        ok, why = osf.data_current(now, date(2026, 9, 29), today0 + 60)
        self.assertFalse(ok)
        self.assertIn("2026-09-29", why)
        ok, why = osf.data_current(now, date(2026, 9, 30), today0 - 60)
        self.assertFalse(ok)
        self.assertIn("chart not updated", why)
        self.assertFalse(osf.data_current(now, None, today0 + 60)[0])

    def test_mu_blends_week_to_date_and_run_rate(self):
        # 3 days known at a = 21% of 3e9; the leaderboard says a = 18% (day)
        # and 20% (trailing 7); the chart's own week-to-date says 21%
        cur = {"a": 0.21 * 3e9, "b": 0.10 * 3e9, "Others": 0.69 * 3e9}
        day = ({"a": 0.18e9, "b": 0.10e9, "c": 0.72e9}, 1e9)
        wk = ({"a": 1.4e9, "b": 0.7e9, "c": 4.9e9}, 7e9)
        f = osf.event_fair("a", self.W, datetime(2026, 10, 1, 3, tzinfo=timezone.utc),
                           self._hist(cur), date(2026, 9, 30), day, wk, {"a": 1.0, "b": 0.5})
        s_rate = (0.18 + 0.20 + 0.21) / 3.0
        want_mu = 100 * (0.21 * 3e9 + 4 * 1e9 * s_rate) / (3e9 + 4 * 1e9)
        self.assertAlmostEqual(f["mu"], want_mu, places=3)
        self.assertEqual((f["known"], f["coverage"], f["complete"]), (3.0, "days", False))
        spread = 100 * (0.21 - 0.18) / 2
        want_sigma = math.sqrt((1.0 * 4 / 7) ** 2 + (spread * 4 / 7) ** 2
                               + osf.SHARE_SIGMA_FLOOR ** 2)
        self.assertAlmostEqual(f["sigma"], want_sigma, places=3)
        self.assertEqual(f["wtd_share"], 21.0)
        self.assertEqual(f["p_ident"], 1.0)                  # < 9 rivals: named
        self.assertEqual(f["data_version"], f"2026-09-28:{int(3e9)}")

    def test_complete_week_and_future_week(self):
        cur = {"a": 0.2 * 7e9, "Others": 0.8 * 7e9}
        day = ({"a": 0.2e9, "c": 0.8e9}, 1e9)
        f = osf.event_fair("a", self.W, datetime(2026, 10, 5, 1, tzinfo=timezone.utc),
                           self._hist(cur), date(2026, 10, 4), day, day, {"a": 1.0})
        self.assertTrue(f["complete"])
        self.assertAlmostEqual(f["mu"], 20.0, places=3)
        self.assertAlmostEqual(f["sigma"], osf.SHARE_SIGMA_FLOOR, places=4)
        # next week, not begun, data through 9/30: gap = 10/5 - 10/1 = 4 days
        g = osf.event_fair("a", date(2026, 10, 5), datetime(2026, 10, 1, 3, tzinfo=timezone.utc),
                           self._hist(), date(2026, 9, 30), day, day, {"a": 1.0})
        self.assertEqual((g["known"], g["coverage"]), (0.0, "not started"))
        self.assertAlmostEqual(g["mu"], 20.0, places=3)
        self.assertAlmostEqual(g["sigma"], math.sqrt((11 / 7) ** 2 + osf.SHARE_SIGMA_FLOOR ** 2), places=3)
        self.assertIsNone(osf.event_fair("a", self.W, datetime(2026, 10, 1, tzinfo=timezone.utc),
                                         [], None, ({}, 0.0), ({}, 0.0), {}))

    def test_identification_against_the_tenth_author(self):
        # ten authors; "j" is 10th at 1.0% while "i" sits 9th at 3.0%
        sh = {"a": 30, "b": 20, "c": 15, "d": 10, "e": 8, "f": 6, "g": 5, "h": 4,
              "i": 3.0, "j": 1.0}
        tot = sum(sh.values())
        counts = {k: v / tot * 1e9 for k, v in sh.items()}
        named = {k: counts[k] * 3 for k in "abcdefghi"}
        named["Others"] = counts["j"] * 3
        now = datetime(2026, 10, 1, 3, tzinfo=timezone.utc)
        hist = self._hist(named)
        lb = (counts, 1e9)
        vol = {k: 0.3 for k in sh}
        fi = osf.event_fair("i", self.W, now, hist, date(2026, 9, 30), lb, lb, vol)
        self.assertEqual(fi["boundary"]["author"], "j")
        self.assertGreater(fi["p_ident"], 0.99)              # 2pp clear, sigma ~0.2
        fj = osf.event_fair("j", self.W, now, hist, date(2026, 9, 30), lb, lb, vol)
        self.assertFalse(fj["named_wtd"])
        self.assertIsNone(fj["wtd_share"])
        self.assertEqual(fj["boundary"]["author"], "i")
        self.assertLess(fj["p_ident"], 0.01)                 # folded into Others
        self.assertAlmostEqual(fj["mu"], 100 * 1.0 / tot, places=2)


class TestWriter(unittest.TestCase):
    NOW = datetime(2026, 10, 1, 3, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osf_")
        self.path = os.path.join(self.tmp, "fair.json")
        self.finals = os.path.join(self.tmp, "finals.jsonl")
        self.preds = os.path.join(self.tmp, "preds.jsonl")
        self.hist = [week("2026-09-07", SEP07), week("2026-09-14", SEP14),
                     week("2026-09-21", SEP21)]
        self.cur = {"deepseek": 606e6, "google": 565e6, "openai": 514e6,
                    "stealth": 345e6, "z-ai": 200e6, "qwen": 175e6,
                    "anthropic": 77.2e6, "xiaomi": 73.5e6, "mistralai": 60e6,
                    "Others": 254.7e6}
        self.day = lb_rows(date(2026, 9, 30), {"deepseek": 192e6, "google": 194e6,
                                               "openai": 181e6, "anthropic": 26e6,
                                               "tencent": 14.7e6, "zz": 338e6})
        self.wk = lb_rows(date(2026, 9, 30), {"deepseek": 1328e6, "google": 1187e6,
                                              "openai": 1034e6, "anthropic": 156e6,
                                              "tencent": 98.6e6, "zz": 2100e6})
        self.events = {"KXANTHSHARE-26OCT05": {"series": "KXANTHSHARE", "author": "anthropic",
                                               "week": "2026-09-28"},
                       "KXGOOGSHARE-26OCT05": {"series": "KXGOOGSHARE", "author": "google",
                                               "week": "2026-09-28"},
                       "KXBADSHARE-26OCT05": {"series": "KXBADSHARE"}}

    def _write(self, now=None, chart_cur=None, **kw):
        weeks = self.hist + [week("2026-09-28", chart_cur or self.cur)]
        args = dict(now=now or self.NOW, chart=(weeks, 1790821510.5), catalog={},
                    day_rows=self.day, week_rows=self.wk, events=self.events,
                    finals_path=self.finals, pred_path=self.preds)
        args.update(kw)
        return osf.write_fair_file(self.path, **args)

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_entries_lag_finals_and_preds(self):
        self.assertEqual(self._write(), (2, 1))
        d = self._read()
        self.assertEqual(d["missing"], ["KXBADSHARE-26OCT05"])
        e = d["entries"]["KXANTHSHARE-26OCT05"]
        self.assertEqual((e["author"], e["week"], e["known"], e["complete"], e["lag"]),
                         ("anthropic", "2026-09-28", 3.0, False, False))
        self.assertTrue(d["data_current"])
        self.assertTrue(osf.LAST["data_current"])
        self.assertAlmostEqual(e["wtd_share"], 100 * 77.2e6 / sum(self.cur.values()), places=3)
        self.assertGreater(e["mu"], 2.5)
        self.assertLess(e["mu"], 3.0)
        # finals: the three settled weeks, once each
        with open(self.finals, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([r["week"] for r in rows], ["2026-09-14", "2026-09-21"])
        self.assertEqual(rows[1]["shares"]["deepseek"], 24.39)
        with open(self.preds, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)
        # a second write 10 min later: no new finals, no new preds, chart
        # unchanged -> first_seen kept
        first = d["chart_seen"]["first_seen"]
        self._write(now=self.NOW + timedelta(minutes=10))
        with open(self.finals, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)
        with open(self.preds, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)
        self.assertEqual(self._read()["chart_seen"]["first_seen"], first)
        # a revision of a complete week is logged again
        self.hist[-1] = week("2026-09-21", dict(SEP21, deepseek=SEP21["deepseek"] + 50e6))
        self._write(now=self.NOW + timedelta(minutes=20))
        with open(self.finals, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual((rows[-1]["week"], rows[-1]["revision"]), ("2026-09-21", True))

    def test_lag_after_midnight_until_both_feeds_move(self):
        self._write()                                         # 10/01 03:00, current
        # 10/02 00:30Z: leaderboard still on 9/30 -> lag
        self.assertEqual(self._write(now=datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc))[0], 2)
        d = self._read()
        self.assertFalse(d["data_current"])
        self.assertTrue(d["entries"]["KXANTHSHARE-26OCT05"]["lag"])
        self.assertIn("leaderboard still on 2026-09-30", d["lag_reason"])
        # 02:20Z: the leaderboard shows 10/01 but the chart has not moved
        day2 = lb_rows(date(2026, 10, 1), {"deepseek": 192e6, "google": 194e6,
                                           "openai": 181e6, "anthropic": 26e6, "zz": 338e6})
        self._write(now=datetime(2026, 10, 2, 2, 20, tzinfo=timezone.utc), day_rows=day2)
        d = self._read()
        self.assertFalse(d["data_current"])
        self.assertIn("chart not updated", d["lag_reason"])
        # 02:30Z: the chart adds 10/01 -> current, 4 days known
        cur2 = {k: v * 4 / 3 for k, v in self.cur.items()}
        self._write(now=datetime(2026, 10, 2, 2, 30, tzinfo=timezone.utc), day_rows=day2,
                    chart_cur=cur2)
        d = self._read()
        self.assertTrue(d["data_current"])
        e = d["entries"]["KXANTHSHARE-26OCT05"]
        self.assertEqual((e["known"], e["lag"]), (4.0, False))
        self.assertEqual(e["data_version"], f"2026-09-28:{int(sum(cur2.values()))}")

    def test_leaderboard_cached_between_reads_and_reread_while_behind(self):
        self._write()
        calls = []
        orig = osf.fetch_rankings
        try:
            osf.fetch_rankings = lambda view: calls.append(view) or (self.day if view == "day" else self.wk)
            osf.write_fair_file(self.path, now=self.NOW + timedelta(minutes=5),
                                chart=(self.hist + [week("2026-09-28", self.cur)], None),
                                events=self.events, finals_path=self.finals,
                                pred_path=self.preds)
            self.assertEqual(calls, [])                      # cached, current
            osf.write_fair_file(self.path, now=datetime(2026, 10, 2, 0, 10, tzinfo=timezone.utc),
                                chart=(self.hist + [week("2026-09-28", self.cur)], None),
                                events=self.events, finals_path=self.finals,
                                pred_path=self.preds)
            self.assertEqual(calls, ["day", "week"])         # behind: re-read
        finally:
            osf.fetch_rankings = orig

    def test_events_from_the_signed_reader(self):
        markets = {"KXANTHSHARE": [
            {"event_ticker": "KXANTHSHARE-26OCT05", "ticker": "KXANTHSHARE-26OCT05-3.1",
             "rules_primary": "If Anthropic scores above 3.1% on OpenRouter text market "
                              "share by model author week of Sep 28, 2026, then ..."},
            {"event_ticker": "KXANTHSHARE-26OCT05", "ticker": "KXANTHSHARE-26OCT05-2.7"}]}
        calls = []

        def reader(path, params):
            calls.append((path, params["series_ticker"]))
            return {"markets": markets.get(params["series_ticker"], [])}
        ev = osf.fetch_events(reader)
        self.assertEqual(ev, {"KXANTHSHARE-26OCT05": {"series": "KXANTHSHARE",
                                                      "author": "anthropic",
                                                      "week": "2026-09-28"}})
        self.assertEqual(len(calls), len(osf.SHARE_SERIES))
        self.assertTrue(all(p == "/markets" for p, _ in calls))

    def test_chart_read_failure_raises(self):
        orig = osf.fetch_market_share
        try:
            def boom():
                raise ValueError("market-share chart: no marketShareData")
            osf.fetch_market_share = boom
            with self.assertRaises(ValueError):
                osf.write_fair_file(self.path, now=self.NOW, events=self.events,
                                    finals_path=self.finals, pred_path=self.preds)
        finally:
            osf.fetch_market_share = orig
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
