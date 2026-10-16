"""Tests for imm_ca_shadow_report.py (the Carbon Arc shadow scorer, 2026-10-16)."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import imm_ca_shadow_report as rep


def _line(t, ts, c=None, d=None):
    r = {"ticker": t, "ts": ts}
    if c is not None:
        r["C"] = {"sides": [["ask", 95]], "by_size": {"200": {"share": 0.1, "usd_day": c},
                                                      "1000": {"share": 0.5, "usd_day": 2 * c}}}
    if d is not None:
        r["D"] = {"sides": [["ask", 96]], "usd_day": d, "eligible": False}
    return r


class TestSummarize(unittest.TestCase):

    def test_each_line_holds_until_the_next_capped(self):
        now = datetime(2026, 10, 16, 6, 0, tzinfo=timezone.utc).timestamp()
        rows = [_line("KXSODAARRAY-26NOV03-T94", "2026-10-16T00:00:00+00:00", c=24.0, d=12.0),
                # an hour later: held 1h at the first line's rates
                _line("KXSODAARRAY-26NOV03-T94", "2026-10-16T01:00:00+00:00", c=48.0),
                # 5h to "now", capped at HOLD_CAP_H (2h)
                ]
        s = rep.summarize(rows, now)
        self.assertAlmostEqual(s["C"]["200"], 24.0 / 24 + 48.0 * 2 / 24)
        self.assertAlmostEqual(s["C"]["1000"], 2 * (24.0 / 24 + 48.0 * 2 / 24))
        self.assertAlmostEqual(s["D"], 12.0 / 24)
        self.assertEqual(s["D_eligible"], 0.0)
        self.assertEqual(s["markets"], {"KXSODAARRAY-26NOV03-T94"})

    def test_load_and_array_vintages_read_the_status_dir(self):
        tmp = tempfile.mkdtemp(prefix="cashadow_")
        with open(os.path.join(tmp, "ca_shadow_2026-10-16.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(_line("KXA-26NOV03-T1", "2026-10-16T00:00:00+00:00", c=1.0)) + "\n")
            f.write("not json\n")
        with open(os.path.join(tmp, "carbon_arc_array_shadow.jsonl"), "w", encoding="utf-8") as f:
            for mu in (90.0, 91.0):
                f.write(json.dumps({"series": "KXSODAARRAY", "month": "2026-10",
                                    "mu": mu, "sigma": 2.0}) + "\n")
        rows = rep.load_lines(tmp, datetime(2026, 10, 15, tzinfo=timezone.utc))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rep.array_vintages(tmp)["KXSODAARRAY"]["2026-10"]["mu"], 91.0)

    def test_settled_values_map_the_print_month_back(self):
        def get_all(path, params, key):
            return [{"expiration_value": "90.4", "close_time": "2026-11-03T04:59:00Z"},
                    {"expiration_value": None, "close_time": "2026-12-03T04:59:00Z"}]
        self.assertEqual(rep.settled_values("KXSODAARRAY", get_all), {"2026-10": 90.4})


if __name__ == "__main__":
    unittest.main()
