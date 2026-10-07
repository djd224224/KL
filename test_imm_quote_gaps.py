"""Tests for imm_quote_gaps (the morning "IMM quotes and overrides" email).

Its own file: importing imm_quote_gaps applies the live launcher's env."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import imm_quote_gaps as g

NOW = datetime(2026, 10, 7, 11, 20, tzinfo=timezone.utc)


class TestConfigGapLines(unittest.TestCase):
    """Jack 2026-10-07: "also resolve issues like this going forward" -- the
    live bot's hand-config gaps go under ACTION NEEDED, one line a series."""

    def test_one_line_per_series(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config_gaps.json")
            self.assertEqual(g.config_gap_lines(NOW, p), [])        # no file
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"updated_at": "2026-10-07T11:00:00Z", "gaps": {
                    "KXA-27": {"series": "KXA", "msg": "no date"},
                    "KXA-28": {"series": "KXA", "msg": "no date"},
                    "KXB-26": {"series": "KXB", "msg": "no election day"}}}, f)
            lines = g.config_gap_lines(NOW, p)
            self.assertIn("3 event(s) in 2 series", lines[0])
            self.assertIn("2026-10-07T11:00:00Z", lines[0])
            self.assertEqual(lines[1:], ["  KXA (+1 more): no date",
                                         "  KXB: no election day", ""])
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"gaps": {}}, f)
            self.assertEqual(g.config_gap_lines(NOW, p), [])
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(g.config_gap_lines(NOW, p), [])


if __name__ == "__main__":
    unittest.main()
