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


def hourly_week(week_start: date, hours: int, share_at, per_hour=1e8, step_min=60):
    """Cumulative chart snapshots of one week every `step_min` minutes from
    its start: share_at(hour) -> {bucket: share} for that stretch (sums to
    1), per_hour requests an hour. Returns (snapshots oldest first, counts now)."""
    t0 = datetime(week_start.year, week_start.month, week_start.day, tzinfo=timezone.utc)
    cum, snaps = {}, []
    n = int(hours * 60 / step_min)
    for i in range(1, n + 1):
        h = (i - 0.5) * step_min / 60.0
        for k, v in share_at(h).items():
            cum[k] = cum.get(k, 0.0) + v * per_hour * step_min / 60.0
        snaps.append({"t": (t0 + timedelta(minutes=step_min * i)).timestamp(),
                      "x": week_start.isoformat(), "ys": dict(cum)})
    return snaps, dict(cum)

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

    def test_known_days_run_to_the_chart_time(self):
        # the chart is live: Thursday noon = 3.5 days of the week known
        thu_noon = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(osf.known_days(self.W, thu_noon, 3.5e9, 1e9), (3.5, "chart time", 3.5))
        self.assertEqual(osf.known_days(self.W, datetime(2026, 9, 27, 23, tzinfo=timezone.utc),
                                        0.0, 1e9), (0.0, "chart time", None))   # not begun
        self.assertEqual(osf.known_days(self.W, datetime(2026, 10, 6, tzinfo=timezone.utc),
                                        7.2e9, 1e9), (7.0, "chart time", 7.0))
        self.assertEqual(osf.known_days(self.W, None, 2e9, 1e9), (2.0, "ratio", 2.0))
        self.assertEqual(osf.known_days(self.W, None, 2e9, 0.0), (0.0, "none", None))

    def test_lag_only_when_a_feed_stalls(self):
        now = datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc)
        fresh = now - timedelta(minutes=20)
        # just after midnight the leaderboard is a day behind -- its normal
        # state until the daily roll -- and the gate keeps quoting
        self.assertEqual(osf.data_current(now, date(2026, 9, 30), fresh), (True, ""))
        self.assertEqual(osf.data_current(now, date(2026, 10, 1), fresh), (True, ""))
        ok, why = osf.data_current(now, date(2026, 9, 29), fresh)      # missed a day
        self.assertFalse(ok)
        self.assertIn("stuck on 2026-09-29", why)
        stale = now - timedelta(minutes=osf.SHARE_CHART_MAX_AGE_MIN + 1)
        ok, why = osf.data_current(now, date(2026, 10, 1), stale)
        self.assertFalse(ok)
        self.assertIn("the chart last moved 10-01 21:29Z", why)
        self.assertFalse(osf.data_current(now, date(2026, 10, 1), None)[0])
        self.assertFalse(osf.data_current(now, None, fresh)[0])

    def test_mu_blends_week_to_date_and_run_rate(self):
        # 3 days known at a = 21% of 3e9; the leaderboard says a = 18% (day)
        # and 20% (trailing 7); the chart's own week-to-date says 21%
        cur = {"a": 0.21 * 3e9, "b": 0.10 * 3e9, "Others": 0.69 * 3e9}
        day = ({"a": 0.18e9, "b": 0.10e9, "c": 0.72e9}, 1e9)
        wk = ({"a": 1.4e9, "b": 0.7e9, "c": 4.9e9}, 7e9)
        thu0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)   # chart time
        f = osf.event_fair("a", self.W, datetime(2026, 10, 1, 3, tzinfo=timezone.utc),
                           self._hist(cur), date(2026, 9, 30), day, wk, {"a": 1.0, "b": 0.5},
                           chart_time=thu0)
        s_rate = (0.18 + 0.20 + 0.21) / 3.0
        want_mu = 100 * (0.21 * 3e9 + 4 * 1e9 * s_rate) / (3e9 + 4 * 1e9)
        self.assertAlmostEqual(f["mu"], want_mu, places=3)
        self.assertEqual((f["known"], f["coverage"], f["complete"], f["ratio_days"]),
                         (3.0, "chart time", False, 3.0))
        spread = 100 * (0.21 - 0.18) / 2
        want_sigma = math.sqrt((1.0 * 4 / 7) ** 2 + (spread * 4 / 7) ** 2
                               + osf.SHARE_SIGMA_FLOOR ** 2)
        self.assertAlmostEqual(f["sigma"], want_sigma, places=3)
        self.assertEqual(f["wtd_share"], 21.0)
        self.assertEqual(f["p_ident"], 1.0)                  # < 9 rivals: named
        # the hold key moves with the leaderboard's day, not the live chart
        self.assertEqual(f["data_version"], "2026-09-28:2026-09-30")
        # twelve hours later the same week-to-date plus half a day: r = 3.5
        cur2 = {k: v * 3.5 / 3 for k, v in cur.items()}
        g = osf.event_fair("a", self.W, datetime(2026, 10, 1, 13, tzinfo=timezone.utc),
                           self._hist(cur2), date(2026, 9, 30), day, wk, {"a": 1.0, "b": 0.5},
                           chart_time=thu0 + timedelta(hours=12))
        rate_t = (1e9 + 1e9 + 3.5e9 / 3.5) / 3
        want_mu = 100 * (0.21 * 3.5e9 + 3.5 * rate_t * s_rate) / (3.5e9 + 3.5 * rate_t)
        self.assertEqual(g["known"], 3.5)
        self.assertAlmostEqual(g["mu"], want_mu, places=3)
        self.assertEqual(g["data_version"], f["data_version"])

    def test_recent_delta_from_the_snapshots(self):
        now = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
        ys_now = {"a": 900.0, "b": 400.0, "c": 50.0, "Others": 650.0}
        hist = [
            {"t": (now - timedelta(hours=40)).timestamp(), "x": "2026-09-28",
             "ys": {"a": 100.0, "b": 100.0, "Others": 300.0}},            # too old
            {"t": (now - timedelta(hours=25)).timestamp(), "x": "2026-09-28",
             "ys": {"a": 500.0, "b": 300.0, "Others": 400.0}},            # nearest 24h
            {"t": (now - timedelta(hours=20)).timestamp(), "x": "2026-09-28",
             "ys": {"a": 600.0, "b": 310.0, "Others": 450.0}},
            {"t": (now - timedelta(hours=24)).timestamp(), "x": "2026-09-21",
             "ys": {"a": 1.0, "Others": 1.0}}]                            # other week
        by, tot, days = osf.recent_delta(hist, self.W, now, ys_now)
        self.assertEqual(by, {"a": 400.0, "b": 100.0})   # "c" named at one end only
        self.assertEqual(tot, 2000.0 - 1200.0)
        self.assertAlmostEqual(days, 25 / 24)
        self.assertIsNone(osf.recent_delta(hist[:1], self.W, now, ys_now))   # none in window
        self.assertIsNone(osf.recent_delta(hist, self.W, None, ys_now))
        self.assertIsNone(osf.recent_delta(hist, date(2026, 10, 5), now, ys_now))

    def test_the_last_24h_joins_the_run_rate(self):
        # week-to-date 3.5 days at a = 21%; the last 24h of the chart ran
        # a = 24%; the leaderboard's last day 18%, trailing 7 days 20%
        thu_noon = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        cur = {"a": 0.21 * 3.5e9, "b": 0.10 * 3.5e9, "Others": 0.69 * 3.5e9}
        then = {"a": 0.21 * 3.5e9 - 0.24e9, "b": 0.10 * 3.5e9 - 0.10e9,
                "Others": 0.69 * 3.5e9 - 0.66e9}
        hist = [{"t": (thu_noon - timedelta(hours=24)).timestamp(), "x": "2026-09-28",
                 "ys": then}]
        day = ({"a": 0.18e9, "b": 0.10e9, "c": 0.72e9}, 1e9)
        wk = ({"a": 1.4e9, "b": 0.7e9, "c": 4.9e9}, 7e9)
        f = osf.event_fair("a", self.W, thu_noon, self._hist(cur), date(2026, 9, 30), day,
                           wk, {"a": 1.0}, chart_time=thu_noon, hist=hist)
        s_rate = (0.18 + 0.20 + 0.24 + 0.21) / 4.0           # all four
        rate_t = (1e9 + 1e9 + 1e9 + 3.5e9 / 3.5) / 4.0
        want = 100 * (0.21 * 3.5e9 + 3.5 * rate_t * s_rate) / (3.5e9 + 3.5 * rate_t)
        self.assertAlmostEqual(f["mu"], want, places=3)
        self.assertEqual((f["recent_share"], f["recent_hours"]), (24.0, 24.0))
        self.assertAlmostEqual(f["rate_spread"], 100 * (0.24 - 0.18) / 2, places=3)
        # without the snapshot the other three carry it
        g = osf.event_fair("a", self.W, thu_noon, self._hist(cur), date(2026, 9, 30), day,
                           wk, {"a": 1.0}, chart_time=thu_noon)
        self.assertIsNone(g["recent_share"])
        self.assertAlmostEqual(g["rate_share"], 100 * (0.18 + 0.20 + 0.21) / 3, places=3)

    def _trend_hist(self, thu_noon, cur):
        # 1e9 requests a day; author a ran 15% over the last 3h, 17% over
        # the last 6h and 20% over the last 24h (falling); b 10% throughout
        def back(hours, a_reqs):
            tot = 1e9 * hours / 24.0
            b = 0.10 * tot
            return {"t": (thu_noon - timedelta(hours=hours)).timestamp(), "x": "2026-09-28",
                    "ys": {"a": cur["a"] - a_reqs, "b": cur["b"] - b,
                           "Others": cur["Others"] - (tot - a_reqs - b)}}
        return [back(24, 0.20e9), back(6, 0.17 * 0.25e9), back(3, 0.15 * 0.125e9)]

    def test_the_chart_run_rate_is_the_week_to_date_pulled_to_the_last_hours(self):
        # 2026-10-05: a named author's run-rate share is its week-to-date
        # (21%) pulled SHARE_RUN_PULL (0.3) toward the chart's last
        # SHARE_RUN_HOURS (6h: 17%) = 19.8%; no spread term, vol alone
        thu_noon = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        cur = {"a": 0.21 * 3.5e9, "b": 0.10 * 3.5e9, "Others": 0.69 * 3.5e9}
        day = ({"a": 0.18e9, "b": 0.10e9, "c": 0.72e9}, 1e9)
        wk = ({"a": 1.4e9, "b": 0.7e9, "c": 4.9e9}, 7e9)
        f = osf.event_fair("a", self.W, thu_noon, self._hist(cur), date(2026, 9, 30), day,
                           wk, {"a": 1.0}, chart_time=thu_noon,
                           hist=self._trend_hist(thu_noon, cur))
        self.assertEqual((f["run_mode"], f["anchor_mode"], f["fast_hours"]),
                         ("wtd+0.3x6h", "week to date", 6.0))
        self.assertAlmostEqual(f["fast_share"], 17.0, places=3)
        self.assertAlmostEqual(f["anchor_share"], 21.0, places=3)
        self.assertAlmostEqual(f["rate_share"], 21.0 + 0.3 * (17.0 - 21.0), places=3)
        self.assertAlmostEqual(f["blend_share"], 100 * (0.18 + 0.20 + 0.20 + 0.21) / 4, places=3)
        self.assertEqual(f["rate_spread"], 0.0)
        self.assertIsNone(f["break_at"])
        rate_t = (1e9 + 1e9 + 1e9 + 3.5e9 / 3.5) / 4.0        # the blend's volume
        want = 100 * (0.21 * 3.5e9 + 3.5 * rate_t * 0.198) / (3.5e9 + 3.5 * rate_t)
        self.assertAlmostEqual(f["mu"], want, places=3)
        want_sigma = math.sqrt((1.0 * 3.5 / 7) ** 2 + osf.SHARE_SIGMA_FLOOR ** 2)
        self.assertAlmostEqual(f["sigma"], want_sigma, places=3)
        # the pull at 1 is the plain last hours (the 10/03 model, minus its spread)
        old = osf.SHARE_RUN_PULL
        osf.SHARE_RUN_PULL = 1.0
        try:
            g = osf.event_fair("a", self.W, thu_noon, self._hist(cur), date(2026, 9, 30), day,
                               wk, {"a": 1.0}, chart_time=thu_noon,
                               hist=self._trend_hist(thu_noon, cur))
        finally:
            osf.SHARE_RUN_PULL = old
        self.assertAlmostEqual(g["rate_share"], 17.0, places=3)

    def test_blend_without_the_last_hours(self):
        thu_noon = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
        cur = {"a": 0.21 * 3.5e9, "b": 0.10 * 3.5e9, "Others": 0.69 * 3.5e9}
        day = ({"a": 0.18e9, "b": 0.10e9, "c": 0.72e9}, 1e9)
        wk = ({"a": 1.4e9, "b": 0.7e9, "c": 4.9e9}, 7e9)
        hist = self._trend_hist(thu_noon, cur)
        blend = 100 * (0.18 + 0.20 + 0.20 + 0.21) / 4
        # only the 24h snapshot stored (a fresh file, the start of a week)
        f = osf.event_fair("a", self.W, thu_noon, self._hist(cur), date(2026, 9, 30), day,
                           wk, {"a": 1.0}, chart_time=thu_noon, hist=hist[:1])
        self.assertEqual((f["run_mode"], f["fast_share"]), ("blend", None))
        self.assertAlmostEqual(f["rate_share"], blend, places=3)
        # the knob at 0 restores the blend everywhere
        old = osf.SHARE_RUN_HOURS
        osf.SHARE_RUN_HOURS = 0
        try:
            g = osf.event_fair("a", self.W, thu_noon, self._hist(cur), date(2026, 9, 30), day,
                               wk, {"a": 1.0}, chart_time=thu_noon, hist=hist)
        finally:
            osf.SHARE_RUN_HOURS = old
        self.assertEqual((g["run_mode"], g["fast_share"]), ("blend", None))
        self.assertAlmostEqual(g["rate_share"], blend, places=3)
        self.assertAlmostEqual(g["rate_spread"], 100 * (0.21 - 0.18) / 2, places=3)
        # an author the chart folds into Others keeps the blend
        h = osf.event_fair("c", self.W, thu_noon, self._hist(cur), date(2026, 9, 30), day,
                           wk, {"a": 1.0}, chart_time=thu_noon, hist=hist)
        self.assertEqual(h["run_mode"], "blend")

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
        # with the chart's timestamp the gap is clock time: 10/5 - 10/1 03:00
        g2 = osf.event_fair("a", date(2026, 10, 5), datetime(2026, 10, 1, 3, tzinfo=timezone.utc),
                            self._hist(), date(2026, 9, 30), day, day, {"a": 1.0},
                            chart_time=datetime(2026, 10, 1, 3, tzinfo=timezone.utc))
        self.assertAlmostEqual(g2["sigma"], math.sqrt(((7 + 3.875) / 7) ** 2
                                                      + osf.SHARE_SIGMA_FLOOR ** 2), places=3)
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
        self._saved = (osf.SHARE_ARCHIVE_DIR, osf.SHARE_LADDER_FILE)
        osf.SHARE_ARCHIVE_DIR = self.tmp
        osf.SHARE_LADDER_FILE = os.path.join(self.tmp, "ladder.jsonl")
        self.events = {"KXANTHSHARE-26OCT05": {"series": "KXANTHSHARE", "author": "anthropic",
                                               "week": "2026-09-28"},
                       "KXGOOGSHARE-26OCT05": {"series": "KXGOOGSHARE", "author": "google",
                                               "week": "2026-09-28"},
                       "KXBADSHARE-26OCT05": {"series": "KXBADSHARE"}}

    def tearDown(self):
        osf.SHARE_ARCHIVE_DIR, osf.SHARE_LADDER_FILE = self._saved

    CACHED = 1790821510.5                     # 2026-10-01 02:25:10.5Z

    def _write(self, now=None, chart_cur=None, cached=CACHED, **kw):
        weeks = self.hist + [week("2026-09-28", chart_cur or self.cur)]
        args = dict(now=now or self.NOW, chart=(weeks, cached), catalog={},
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
        self.assertEqual((e["author"], e["week"], e["complete"], e["lag"]),
                         ("anthropic", "2026-09-28", False, False))
        self.assertAlmostEqual(e["known"], 3 + (2 * 3600 + 25 * 60 + 10.5) / 86400, places=3)
        self.assertEqual(d["chart_cached_at"], "2026-10-01T02:25:10.500000+00:00")
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
        # a second write 10 min later on a chart that moved: no new finals,
        # no new preds, and the hold key stays put
        self._write(now=self.NOW + timedelta(minutes=10),
                    chart_cur={k: v * 1.01 for k, v in self.cur.items()},
                    cached=self.CACHED + 600)
        with open(self.finals, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)
        with open(self.preds, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)
        self.assertEqual(self._read()["entries"]["KXANTHSHARE-26OCT05"]["data_version"],
                         e["data_version"])
        # a revision of a complete week is logged again
        self.hist[-1] = week("2026-09-21", dict(SEP21, deepseek=SEP21["deepseek"] + 50e6))
        self._write(now=self.NOW + timedelta(minutes=20))
        with open(self.finals, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual((rows[-1]["week"], rows[-1]["revision"]), ("2026-09-21", True))

    def test_lag_only_while_the_chart_stalls(self):
        t1 = datetime(2026, 10, 2, 0, 30, tzinfo=timezone.utc)
        # 10/02 00:30Z, chart 10 minutes old, leaderboard still on 9/30 (its
        # daily roll not yet in): quotes, keyed to the 9/30 day
        self._write(now=t1, cached=(t1 - timedelta(minutes=10)).timestamp())
        d = self._read()
        self.assertTrue(d["data_current"])
        e = d["entries"]["KXANTHSHARE-26OCT05"]
        self.assertEqual((e["lag"], e["data_version"]), (False, "2026-09-28:2026-09-30"))
        self.assertAlmostEqual(e["known"], 4 + 20 / 1440, places=3)
        # 02:20Z: the leaderboard rolls to 10/01 -> the hold key moves once
        day2 = lb_rows(date(2026, 10, 1), {"deepseek": 192e6, "google": 194e6,
                                           "openai": 181e6, "anthropic": 26e6, "zz": 338e6})
        t2 = datetime(2026, 10, 2, 2, 20, tzinfo=timezone.utc)
        self._write(now=t2, day_rows=day2, cached=(t2 - timedelta(minutes=5)).timestamp())
        e = self._read()["entries"]["KXANTHSHARE-26OCT05"]
        self.assertEqual((e["lag"], e["data_version"]), (False, "2026-09-28:2026-10-01"))
        # 06:00Z with the chart stuck at 02:15Z: lag, the gate stands aside
        t3 = datetime(2026, 10, 2, 6, 0, tzinfo=timezone.utc)
        self._write(now=t3, day_rows=day2, cached=(t2 - timedelta(minutes=5)).timestamp())
        d = self._read()
        self.assertFalse(d["data_current"])
        self.assertFalse(osf.LAST["data_current"])
        self.assertTrue(d["entries"]["KXANTHSHARE-26OCT05"]["lag"])
        self.assertIn("the chart last moved 10-02 02:15Z", d["lag_reason"])

    def test_snapshots_kept_once_per_chart_time_for_48h(self):
        self._write()
        self._write(now=self.NOW + timedelta(minutes=5))           # same cachedAt
        d = self._read()
        self.assertEqual([h["t"] for h in d["chart_hist"]], [self.CACHED])
        self.assertIsNone(d["entries"]["KXANTHSHARE-26OCT05"]["recent_share"])
        # a day later the chart moved: the 24h run rate comes from the delta
        later = {k: v * 4.5 / 3.1 for k, v in self.cur.items() if k != "anthropic"}
        d_others = sum(later.values()) - (sum(self.cur.values()) - self.cur["anthropic"])
        later["anthropic"] = self.cur["anthropic"] + 0.03 * d_others / 0.97   # 3% of the day
        self._write(now=self.NOW + timedelta(hours=24), chart_cur=later,
                    cached=self.CACHED + 86400)
        d = self._read()
        self.assertEqual(len(d["chart_hist"]), 2)
        e = d["entries"]["KXANTHSHARE-26OCT05"]
        self.assertAlmostEqual(e["recent_share"], 3.0, places=2)
        self.assertEqual(e["recent_hours"], 24.0)
        # 49h on, the first snapshot has aged out
        self._write(now=self.NOW + timedelta(hours=49), chart_cur=later,
                    cached=self.CACHED + 49 * 3600)
        self.assertEqual([h["t"] for h in self._read()["chart_hist"]],
                         [self.CACHED + 86400, self.CACHED + 49 * 3600])

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


class TestBreaks(unittest.TestCase):
    """2026-10-05: breaks in the chart's mix and revisions of its counts."""
    W = date(2026, 10, 5)
    DAY = ({"a": 0.2e9, "b": 0.1e9, "c": 0.7e9}, 1e9)

    @staticmethod
    def stealth_gone(at_hour):
        # a 20%, b 10%, s (a stealth model) 10%, Others 60% -- until s stops
        # at `at_hour` and the rest renormalize (a -> 22.2%)
        def f(h):
            if h < at_hour:
                return {"a": 0.20, "b": 0.10, "s": 0.10, "Others": 0.60}
            return {"a": 0.20 / 0.9, "b": 0.10 / 0.9, "s": 0.0, "Others": 0.60 / 0.9}
        return f

    def _now(self, snaps):
        return datetime.fromtimestamp(snaps[-1]["t"], timezone.utc)

    def test_a_withdrawn_model_is_a_break_at_its_change_point(self):
        snaps, cur = hourly_week(self.W, 40, self.stealth_gone(30), step_min=10)
        b = osf.detect_break(snaps, self.W, self._now(snaps), cur)
        self.assertEqual(b["bucket"], "s")
        self.assertAlmostEqual(b["old"], 10.0, places=3)
        self.assertEqual(b["new"], 0.0)
        t30 = datetime(2026, 10, 6, 6, tzinfo=timezone.utc).timestamp()
        self.assertEqual(b["t"], t30)                       # the snapshot it stopped at
        self.assertEqual(b["ys"], next(h["ys"] for h in snaps if h["t"] == t30))
        # the run rate anchors on the flow since it: a 22.2%, not its 21.0% week
        vol = {"a": 1.0}
        f = osf.event_fair("a", self.W, self._now(snaps), [week(self.W.isoformat(), cur)],
                           date(2026, 10, 5), self.DAY, self.DAY, vol,
                           chart_time=self._now(snaps), hist=snaps, anchor=b)
        self.assertEqual((f["anchor_mode"], f["run_mode"]), ("since break", "break+0.3x6h"))
        self.assertAlmostEqual(f["anchor_share"], 20 / 0.9, places=3)
        self.assertAlmostEqual(f["rate_share"], 20 / 0.9, places=3)   # last 6h agree
        wtd = 100 * cur["a"] / sum(cur.values())
        self.assertAlmostEqual(f["rate_spread"], (20 / 0.9 - wtd) / 2, places=3)
        self.assertEqual(f["break_at"], "2026-10-06T06:00:00+00:00")
        # without the anchor: the week-to-date, a point low for the rest of the week
        g = osf.event_fair("a", self.W, self._now(snaps), [week(self.W.isoformat(), cur)],
                           date(2026, 10, 5), self.DAY, self.DAY, vol,
                           chart_time=self._now(snaps), hist=snaps)
        self.assertAlmostEqual(g["anchor_share"], wtd, places=3)
        self.assertLess(g["mu"], f["mu"])

    def test_a_fresh_break_runs_on_the_last_hours_alone(self):
        snaps, cur = hourly_week(self.W, 32, self.stealth_gone(30), step_min=10)
        b = osf.detect_break(snaps, self.W, self._now(snaps), cur)
        self.assertEqual(b["bucket"], "s")
        f = osf.event_fair("a", self.W, self._now(snaps), [week(self.W.isoformat(), cur)],
                           date(2026, 10, 5), self.DAY, self.DAY, {"a": 1.0},
                           chart_time=self._now(snaps), hist=snaps, anchor=b)
        self.assertEqual((f["anchor_mode"], f["run_mode"]), ("break, last hours", "chart 6h"))
        self.assertAlmostEqual(f["rate_share"], f["fast_share"], places=6)

    def test_no_break_on_a_swing_at_the_week_start_or_without_a_base(self):
        # google's Saturday: 21.7% -> 26% for three hours is a swing (17%)
        def swing(h):
            a = 0.26 if h >= 37 else 0.217
            return {"a": a, "Others": 1 - a}
        snaps, cur = hourly_week(self.W, 40, swing)
        self.assertIsNone(osf.detect_break(snaps, self.W, self._now(snaps), cur))
        # a launch at the week's turn: the week-to-date already holds it
        snaps, cur = hourly_week(self.W, 40, lambda h: {"z": 0.21, "Others": 0.79})
        self.assertIsNone(osf.detect_break(snaps, self.W, self._now(snaps), cur))
        # the week's first hours: no base to break from
        snaps, cur = hourly_week(self.W, 8, self.stealth_gone(6))
        self.assertIsNone(osf.detect_break(snaps, self.W, self._now(snaps), cur))
        # and none again from the break itself once the flow since it is steady
        snaps, cur = hourly_week(self.W, 48, self.stealth_gone(30), step_min=10)
        early = snaps[:6 * 34]                          # 34h: the break is in view
        b = osf.detect_break(early, self.W, self._now(early), early[-1]["ys"])
        self.assertEqual(b["bucket"], "s")
        self.assertIsNone(osf.detect_break(snaps, self.W, self._now(snaps), cur, base=b))

    def test_revisions_but_not_the_named_set_moving(self):
        snaps, _ = hourly_week(self.W, 10, lambda h: {"a": 0.2, "s": 0.1, "m": 0.02,
                                                       "Others": 0.68})
        now = snaps[-1]["t"]
        self.assertIsNone(osf.chart_revision(snaps, self.W, now))
        # s's requests re-attributed to a: s vanishes, a jumps
        moved = [dict(h, ys=dict(h["ys"])) for h in snaps]
        last = moved[-1]["ys"]
        last["a"] += last.pop("s")
        self.assertEqual(osf.chart_revision(moved, self.W, now), (now, last))
        # a named author's count falls
        fell = [dict(h, ys=dict(h["ys"])) for h in snaps]
        fell[-1]["ys"]["a"] = fell[-2]["ys"]["a"] - 1e6
        self.assertEqual(osf.chart_revision(fell, self.W, now)[0], now)
        # m drops out of the nine named: Others takes it in -- not a revision
        churn = [dict(h, ys=dict(h["ys"])) for h in snaps]
        churn[-1]["ys"]["Others"] += churn[-1]["ys"].pop("m")
        self.assertIsNone(osf.chart_revision(churn, self.W, now))
        # 10/06 01:43Z: the ninth name swaps -- m (2%) out to Others, n (from
        # Others, a hair bigger) in; Others hardly moves -- not a revision
        swap = [dict(h, ys=dict(h["ys"])) for h in snaps]
        m = swap[-1]["ys"].pop("m")
        swap[-1]["ys"]["n"] = m * 1.004
        swap[-1]["ys"]["Others"] += m - m * 1.004
        self.assertIsNone(osf.chart_revision(swap, self.W, now))
        # s (10%) leaves beside m's 2% and a newcomer arrives holding its
        # count: re-attributed to a maker the chart did not name -- a revision
        newco = [dict(h, ys=dict(h["ys"])) for h in snaps]
        newco[-1]["ys"]["maker"] = newco[-1]["ys"].pop("s")
        self.assertEqual(osf.chart_revision(newco, self.W, now)[0], now)
        # older than the hold: forgotten
        self.assertIsNone(osf.chart_revision(fell, self.W,
                                             now + (osf.SHARE_REVISION_HOLD_HOURS + 1) * 3600))


class TestWriterBreaks(unittest.TestCase):
    W = date(2026, 10, 5)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="osf_brk_")
        self.path = os.path.join(self.tmp, "fair.json")
        self._saved = (osf.SHARE_ARCHIVE_DIR, osf.SHARE_LADDER_FILE)
        osf.SHARE_ARCHIVE_DIR = self.tmp
        osf.SHARE_LADDER_FILE = os.path.join(self.tmp, "ladder.jsonl")
        self.day = lb_rows(date(2026, 10, 6), {"a": 0.2e9, "b": 0.1e9, "zz": 0.7e9})
        self.events = {"KXASHARE-26OCT12": {"series": "KXASHARE", "author": "a",
                                            "week": "2026-10-05"}}
        # 80h of the week, a snapshot every 20 minutes; s stops at 30h
        self.snaps, _ = hourly_week(self.W, 80, TestBreaks.stealth_gone(30), step_min=20)
        self.prior = [week((self.W - timedelta(days=7 * i)).isoformat(),
                           {"a": 2e8, "b": 1e8, "s": 1e8, "Others": 6e8})
                      for i in range(8, 0, -1)]

    def tearDown(self):
        osf.SHARE_ARCHIVE_DIR, osf.SHARE_LADDER_FILE = self._saved

    def _write_at(self, i, **kw):
        snap = self.snaps[i]
        now = datetime.fromtimestamp(snap["t"] + 60, timezone.utc)
        args = dict(now=now, chart=(self.prior + [week(snap["x"], snap["ys"])], snap["t"]),
                    catalog={}, day_rows=self.day, week_rows=self.day, events=self.events,
                    finals_path=os.path.join(self.tmp, "finals.jsonl"),
                    pred_path=os.path.join(self.tmp, "preds.jsonl"))
        args.update(kw)
        osf.write_fair_file(self.path, **args)
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def _archived(self):
        days = sorted(x for x in os.listdir(self.tmp) if x.startswith("openrouter_share_chart_"))
        n = 0
        for x in days:
            with open(os.path.join(self.tmp, x), encoding="utf-8") as f:
                n += len(f.readlines())
        return days, n

    def test_break_found_kept_past_the_snapshots_and_dropped_next_week(self):
        brk_t = datetime(2026, 10, 6, 6, tzinfo=timezone.utc).timestamp()
        d = None
        for i in range(len(self.snaps)):              # every snapshot for 80h
            d = self._write_at(i)
            if i == 3 * 34 - 1:                        # 34h: found, at the change point
                self.assertEqual(d["breaks"]["2026-10-05"]["t"], brk_t)
                self.assertEqual(d["entries"]["KXASHARE-26OCT12"]["anchor_mode"],
                                 "since break")
        # 80h on, the 48h of snapshots start after the break: still anchored
        self.assertGreater(d["chart_hist"][0]["t"], brk_t)
        self.assertEqual(d["breaks"]["2026-10-05"]["t"], brk_t)
        e = d["entries"]["KXASHARE-26OCT12"]
        self.assertAlmostEqual(e["anchor_share"], 20 / 0.9, places=3)
        self.assertTrue(e["break_at"].startswith("2026-10-06T06:00"))
        self.assertEqual(d["model"]["run_pull"], osf.SHARE_RUN_PULL)
        # each snapshot archived once, by its UTC day
        days, n = self._archived()
        self.assertEqual(days, ["openrouter_share_chart_2026-10-05.jsonl",
                                "openrouter_share_chart_2026-10-06.jsonl",
                                "openrouter_share_chart_2026-10-07.jsonl",
                                "openrouter_share_chart_2026-10-08.jsonl"])
        self.assertEqual(n, len(self.snaps))
        self._write_at(len(self.snaps) - 1)            # same cachedAt: not again
        self.assertEqual(self._archived()[1], len(self.snaps))
        # next week's chart: the break is gone
        nxt, _ = hourly_week(self.W + timedelta(days=7), 2, lambda h: {"a": 0.2, "Others": 0.8})
        snap = nxt[-1]
        d = self._write_at(0, now=datetime.fromtimestamp(snap["t"] + 60, timezone.utc),
                           chart=(self.prior + [week(snap["x"], snap["ys"])], snap["t"]),
                           events={})
        self.assertEqual(d["breaks"], {})

    def test_revision_stands_the_family_aside_and_anchors_after_it(self):
        for i in range(3 * 20):
            self._write_at(i)
        snap = self.snaps[3 * 20]
        ys = dict(snap["ys"])
        ys["a"] += ys.pop("s")                          # re-attributed
        now = datetime.fromtimestamp(snap["t"] + 60, timezone.utc)
        d = self._write_at(3 * 20, now=now,
                           chart=(self.prior + [week(snap["x"], ys)], snap["t"]))
        self.assertFalse(d["data_current"])
        self.assertIn("revised the week's counts", d["lag_reason"])
        self.assertTrue(d["entries"]["KXASHARE-26OCT12"]["lag"])
        self.assertEqual((d["breaks"]["2026-10-05"]["bucket"], d["breaks"]["2026-10-05"]["t"]),
                         ("revision", snap["t"]))
        self.assertFalse(osf.LAST["data_current"])

    def test_a_kept_revision_no_longer_found_is_dropped_and_the_break_refound(self):
        # 10/06 02:17Z: the first deploy kept a "revision" at the ninth-name
        # swap; the fixed rules drop it and re-find the 06:00Z break
        i = 3 * 36 - 1                                  # 36h, the break in view
        for j in range(i):
            self._write_at(j)
        d = self._write_at(i)
        good = d["breaks"]["2026-10-05"]
        self.assertEqual(good["bucket"], "s")
        bogus = dict(good, bucket="revision", t=self.snaps[i - 3]["t"],
                     ys=self.snaps[i - 3]["ys"], old=None, new=None)
        d["breaks"] = {"2026-10-05": bogus}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(d, f)
        d = self._write_at(i + 1)
        self.assertTrue(d["data_current"])
        self.assertEqual((d["breaks"]["2026-10-05"]["bucket"], d["breaks"]["2026-10-05"]["t"]),
                         ("s", good["t"]))
        self.assertEqual(d["entries"]["KXASHARE-26OCT12"]["anchor_mode"], "since break")

    def test_ladder_logged_on_each_event_read(self):
        markets = [{"event_ticker": "KXANTHSHARE-26OCT12", "ticker": "KXANTHSHARE-26OCT12-2.4",
                    "yes_bid_dollars": "0.6600", "yes_ask_dollars": "0.6700",
                    "last_price_dollars": "0.6700", "volume": 1200, "open_interest": 800},
                   {"event_ticker": "KXANTHSHARE-26OCT12", "ticker": "KXANTHSHARE-26OCT12-2.9",
                    "yes_bid": 12, "yes_ask": 14, "volume": 300}]

        def reader(path, params):
            return {"markets": markets if params["series_ticker"] == "KXANTHSHARE" else []}
        self._write_at(10, events=None, get_json=reader)
        with open(osf.SHARE_LADDER_FILE, encoding="utf-8") as f:
            rows = [json.loads(x) for x in f]
        self.assertEqual([(r["ticker"], r["yes_bid"], r["yes_ask"]) for r in rows],
                         [("KXANTHSHARE-26OCT12-2.4", 66.0, 67.0),
                          ("KXANTHSHARE-26OCT12-2.9", 12.0, 14.0)])
        self.assertEqual((rows[0]["last"], rows[0]["volume"], rows[0]["open_interest"]),
                         (67.0, 1200, 800))
        self.assertTrue(rows[0]["at"])
        # the cached events (within the hour) read nothing and log nothing
        self._write_at(11, events=None, get_json=reader)
        with open(osf.SHARE_LADDER_FILE, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)

if __name__ == "__main__":
    unittest.main()
