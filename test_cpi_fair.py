"""Tests for cpi_fair (the CPI pilot's fair-value writer, 2026-10-11)."""
import json
import math
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import cpi_fair as c


def idx_from(sa: dict, nsa: dict) -> dict:
    return {c.CORE_SA: dict(sa), c.CORE_NSA: dict(nsa)}


def steady(start="2024-01", months=33, level=300.0, mom=0.25, skip=()):
    """An index rising `mom`% a month from `start`, with months in `skip`
    left out (October 2025 was never published)."""
    out, v, m = {}, level, start
    for _ in range(months):
        if m not in skip:
            out[m] = round(v, 3)
        v *= 1 + mom / 100.0
        m = c.month_add(m, 1)
    return out


class TestBasics(unittest.TestCase):
    def test_month_math_and_tickers(self):
        self.assertEqual(c.month_add("2026-11", 2), "2027-01")
        self.assertEqual(c.month_add("2026-01", -13), "2024-12")
        self.assertEqual(c.event_month("KXCPICORE-26NOV"), "2026-11")
        self.assertEqual(c.event_month("KXCPICOREYOY-27JAN"), "2027-01")
        self.assertIsNone(c.event_month("KXCPICORE-26XYZ"))
        self.assertIsNone(c.event_month("KXCPICORE"))

    def test_sigma_steps_by_horizon(self):
        self.assertEqual(c.sigma("mom", 2), .124)
        self.assertEqual(c.sigma("yoy", 28), .117)
        self.assertEqual(c.sigma("yoy", 29), .189)
        self.assertEqual(c.sigma("yoy", 500), .413)

    def test_rounding_boundary(self):
        # YES iff the unrounded change >= K + 0.05: an X of exactly 0.25 is 0.3
        self.assertAlmostEqual(c.p_yes(0.2, 0.25, 0.124), 0.5, places=9)
        self.assertGreater(c.p_yes(0.2, 0.30, 0.124), 0.5)
        self.assertLess(c.p_yes(0.3, 0.30, 0.124), 0.5)

    def test_parse_bls_drops_annual_rows(self):
        js = {"Results": {"series": [
            {"seriesID": c.CORE_SA, "data": [
                {"year": "2026", "period": "M08", "value": "331.5"},
                {"year": "2025", "period": "M13", "value": "320.0"},
                {"year": "2026", "period": "M07", "value": "x"}]}]}}
        self.assertEqual(c.parse_bls(js), {c.CORE_SA: {"2026-08": 331.5}})


class TestMu(unittest.TestCase):
    def setUp(self):
        # Jan 2024 .. Sep 2026 (33 months) at 0.25%/month; Oct 2025 missing
        self.sa = steady(skip=("2025-10",))
        self.nsa = steady(mom=0.24, skip=("2025-10",))
        self.idx = idx_from(self.sa, self.nsa)

    def test_mom_forecast_is_the_12_month_geometric_average(self):
        f = c.mom_forecast(self.idx, "2026-08")
        self.assertAlmostEqual(f, 0.25, places=3)
        self.assertEqual(c.event_mu("mom", "2026-11", self.idx, "2026-08"), f)
        self.assertIsNone(c.event_mu("mom", "2026-08", self.idx, "2026-08"))

    def test_october_2025_gap(self):
        self.assertEqual(c.year_ago_mom(self.idx, "2025-10"), 0.0)
        two = (self.sa["2025-11"] / self.sa["2025-09"] - 1) * 100
        self.assertAlmostEqual(c.year_ago_mom(self.idx, "2025-11"), two, places=9)
        self.assertAlmostEqual(c.year_ago_mom(self.idx, "2025-09"), 0.25, places=2)

    def test_yoy_carries_the_last_print_forward(self):
        last = "2026-08"
        yoy_l = (self.nsa[last] / self.nsa["2025-08"] - 1) * 100
        f = c.mom_forecast(self.idx, last)
        mu = c.event_mu("yoy", "2026-09", self.idx, last)
        self.assertAlmostEqual(mu, yoy_l + f - c.year_ago_mom(self.idx, "2025-09"), places=9)
        # across the gap: Oct (base 0) and Nov (base = the two-month change)
        mu_nov = c.event_mu("yoy", "2026-11", self.idx, last)
        expect = yoy_l + sum(f - c.year_ago_mom(self.idx, b)
                             for b in ("2025-09", "2025-10", "2025-11"))
        self.assertAlmostEqual(mu_nov, expect, places=9)
        self.assertIsNone(c.event_mu("yoy", "2026-11", idx_from(self.sa, {}), last))


class TestWrite(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "cpi_fair.json")
        self.now = datetime(2026, 10, 12, 1, 0, tzinfo=timezone.utc)
        sa = steady(skip=("2025-10",), months=32)          # through 2026-08
        nsa = steady(mom=0.24, skip=("2025-10",), months=32)
        self.idx = idx_from(sa, nsa)
        self.calls = 0
        close = (self.now + timedelta(days=60)).isoformat()
        self.events = {
            "KXCPICORE-26NOV": {"series": "KXCPICORE", "kind": "mom", "month": "2026-11",
                                "close": close, "strikes": {"KXCPICORE-26NOV-T0.1": 0.1,
                                                            "KXCPICORE-26NOV-T0.3": 0.3}},
            "KXCPICOREYOY-26NOV": {"series": "KXCPICOREYOY", "kind": "yoy",
                                   "month": "2026-11", "close": close,
                                   "strikes": {"KXCPICOREYOY-26NOV-T2.9": 2.9}},
            "KXCPICORE-26AUG": {"series": "KXCPICORE", "kind": "mom", "month": "2026-08",
                                "close": close, "strikes": {"KXCPICORE-26AUG-T0.2": 0.2}}}

    def bls(self, now):
        self.calls += 1
        return self.idx

    def test_writes_markets_in_the_loaders_shape(self):
        ok, miss = c.write_fair_file(self.path, now=self.now, events=self.events,
                                     bls_fn=self.bls)
        self.assertEqual((ok, miss), (2, 1))                 # Aug is already printed
        with open(self.path, encoding="utf-8") as f:
            d = json.load(f)
        m = d["markets"]["KXCPICORE-26NOV-T0.1"]
        self.assertEqual(m["event"], "KXCPICORE-26NOV")
        self.assertEqual(m["fetched_at"], self.now.isoformat())
        mu = d["events"]["KXCPICORE-26NOV"]["mu"]
        sd = c.sigma("mom", 60)
        self.assertAlmostEqual(m["p"], round(c.p_yes(0.1, mu, sd), 4), places=4)
        self.assertGreater(m["p"], d["markets"]["KXCPICORE-26NOV-T0.3"]["p"])
        self.assertEqual(d["last_print"], "2026-08")
        self.assertNotIn("KXCPICORE-26AUG-T0.2", d["markets"])

    def test_bls_is_cached_and_a_failed_read_keeps_the_cache(self):
        c.write_fair_file(self.path, now=self.now, events=self.events, bls_fn=self.bls)
        c.write_fair_file(self.path, now=self.now + timedelta(hours=1),
                          events=self.events, bls_fn=self.bls)
        self.assertEqual(self.calls, 1)
        def boom(now):
            raise RuntimeError("down")
        later = self.now + timedelta(hours=c.BLS_REFRESH_HOURS + 1)
        ok, _ = c.write_fair_file(self.path, now=later, events=self.events, bls_fn=boom)
        self.assertEqual(ok, 2)
        with self.assertRaises(RuntimeError):
            c.write_fair_file(os.path.join(self.dir, "fresh.json"), now=later,
                              events=self.events, bls_fn=boom)


if __name__ == "__main__":
    unittest.main()
