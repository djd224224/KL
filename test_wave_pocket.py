"""wave_pocket tab names and the compact fills chart. No network, no files."""
import unittest
from datetime import datetime, timezone

import wave_pocket as wp

NOW = datetime(2026, 11, 11, 17, 30, tzinfo=timezone.utc)          # 12:30 PM ET 11/11
DASHES = [("w2028", 8773, "2028 election wave", None), ("pol1106", 8771, "Political wave 11/06", None),
          ("e1104", 8774, "11/04 election wave", None),
          ("w1109-control", 8777, "w1109-control", "2026-11-09T14:08:09Z"),
          ("w1111-elec2028", 8778, "w1111-elec2028", "2026-11-11T12:13:09Z"),
          ("w1111-w28", 8779, "w1111-w28", "2026-11-11T13:31:38Z"),
          ("w1111-pol", 8780, "w1111-pol", "2026-11-11T13:31:39Z"),
          ("w1111-statenom", 8781, "w1111-statenom", "2026-11-11T16:08:15Z")]


class TabNames(unittest.TestCase):
    def test_todays_two_waves_lead_and_say_so(self):
        """Jack 2026-11-11: "Name them clearly in wave pocket. I can't tell which are the 2
        waves from today" """
        got = wp.name_dashboards(DASHES, NOW)
        self.assertEqual([t for _i, _p, t, _a in got], [
            "Today #1 · 2028 primaries",
            "Today #2 · Senate/Gov nominees + state races",
            "11/09 · 2030 Senate & House control",
            "Relight group · old 2028 House races",
            "Relight group · 11/06 politics",
            "Since 11/06 · every 2028 market",
            "Since 11/06 · politics",
            "Since 11/06 · 11/04 wave + other listed"])
        about = {i: a for i, _p, _t, a in got}
        self.assertTrue(about["w1111-statenom"].endswith("(w1111-statenom, :8781)"))
        self.assertIn("INCLUDES today's 2028 primaries", about["w2028"])
        # the next day the same waves carry their date, not "Today"
        nxt = wp.name_dashboards(DASHES, datetime(2026, 11, 12, 17, 0, tzinfo=timezone.utc))
        self.assertEqual(nxt[0][2], "11/11 · Senate/Gov nominees + state races")   # newest first
        self.assertEqual(nxt[1][2], "11/11 · 2028 primaries")

    def test_an_unnamed_new_wave(self):
        got = wp.name_dashboards(DASHES + [("w1111-foo", 8782, "Foo things", "2026-11-11T17:00:00Z")], NOW)
        self.assertEqual(got[2][2], "Today #3 · Foo things")
        self.assertEqual(got[2][3], "(w1111-foo, :8782)")


if __name__ == "__main__":
    unittest.main()
