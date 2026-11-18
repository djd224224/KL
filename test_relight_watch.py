"""relight_watch.new_waves: a registry wave the 24/7 wave check added inside the
window is reported with its listed markets (2026-11-11: the +803 program jump of
w1111-statenom read as "0 listed"). Temp files only."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import relight_watch as rw


class NewWaves(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="relight-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        reg = {"waves": {
            "w-old": {"series": ["KXOLD"], "added": "2026-11-11T12:13:09Z", "note": "hand"},
            "w-new": {"series": ["KXSENATENOM"], "near": ["KXGOVNOM"], "added": "2026-11-11T16:08:15Z",
                      "note": "auto: 803 new programs"}}}
        lst = {"markets": {"KXSENATENOM-ALD-28-DJON": {"est_usd_day": 17.27},
                           "KXGOVNOM-AK-28-R": {"est_usd_day": 2.5},
                           "KXOLD-28-A": {"est_usd_day": 9.0}}}
        self.reg, self.lst = os.path.join(self.tmp, "waves.json"), os.path.join(self.tmp, "pvd.json")
        json.dump(reg, open(self.reg, "w", encoding="utf-8"))
        json.dump(lst, open(self.lst, "w", encoding="utf-8"))

    def test_window(self):
        with mock.patch.object(rw, "REGISTRY", self.reg), mock.patch.object(rw, "LIST", self.lst):
            got = rw.new_waves("2026-11-11 16:01:14Z")
            self.assertEqual([(w["name"], w["markets"], w["usd_day"]) for w in got], [("w-new", 2, 19.77)])
            self.assertEqual(got[0]["series"], ["KXGOVNOM", "KXSENATENOM"])
            self.assertEqual(rw.new_waves("2026-11-11 16:08:15Z"), [])      # added AT since: an older check's
            self.assertEqual(len(rw.new_waves("2026-11-11 00:00:00Z")), 2)
        with mock.patch.object(rw, "REGISTRY", os.path.join(self.tmp, "absent.json")):
            self.assertEqual(rw.new_waves("0000"), [])


# the waves task's log of the 11/18 05:00Z pass, as it was (no "taking" line for
# w1111-elec2028: it fell out of the tail) and around it a pass that had one
ELEC_OLD = """===== Wed 11/18/2026  0:00:19.33 pass 7: registry waves
== wave w1111-elec2028 main 2026-11-18T05:00:21Z: scan (write)
    2026-11-18 05:07:10Z   RELIT  KXRPRESPRIMARY-28CO-MRUB live last None book 17x21 size 200/200
    2026-11-18 05:07:10Z   RELIT  KXDPRESPRIMARY-28VA-JOSS live last None book 24x27 size 200/200
    2026-11-18 05:07:11Z WROTE 975 entries -> C:\\KL\\post_vote_dead_books.json (now 6159 listed)
== wave w1111-elec2028 main: tune
    WROTE 2026-11-18T05:07:12Z 4 series since 2026-11-09T16:40:00Z: 976 live entries
== wave w1111-pol main 2026-11-18T05:07:32Z: scan (write)
    2026-11-18 05:07:37Z programs 5869 | election family: 2 listed for this period
    2026-11-18 05:07:43Z scored 1 clearing $1.10 | skipped {'no book': 0, 'no quotable side': 0, 'under bar': 0} | taking 1 by ROI (extended 0, relit 1, new 0), new at risk if filled $192 (budget $400,000), ~$2/day; 0 over budget
    2026-11-18 05:07:43Z   RELIT  KXDCSTATEHOODHOUSE-26NOV-27JUL01 live last 44.0
    2026-11-18 05:07:44Z WROTE 1 entries -> C:\\KL\\post_vote_dead_books.json (now 6159 listed)
"""
# the same pass once the task log keeps the summary lines (pvd_waves.run_lines)
ELEC_NEW = ELEC_OLD.replace(
    "    2026-11-18 05:07:10Z   RELIT  KXRPRESPRIMARY-28CO-MRUB",
    "    2026-11-18 05:00:24Z programs 5869 | election family: 1 listed for this period\n"
    "    2026-11-18 05:07:10Z scored 975 clearing $1.10 | skipped {'no book': 0, 'no quotable side': 0, "
    "'under bar': 1} | taking 975 by ROI (extended 0, relit 975, new 0), new at risk if filled $231,130 "
    "(budget $400,000), ~$600/day; 0 over budget\n"
    "    2026-11-18 05:07:10Z   RELIT  KXRPRESPRIMARY-28CO-MRUB")


class ScanCounts(unittest.TestCase):
    """Jack 2026-11-18: "i didnt get an email and text confirming the relight" -- the
    05:28Z alert said "4 markets listed" for the 976-market w1111-elec2028 relight."""

    def counts(self, text):
        tmp = tempfile.mkdtemp(prefix="relight-scan-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "waves-task.log")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        with mock.patch.object(rw, "LOGS", [path]):
            return rw.scan("2026-11-18 04:50:00Z")[0]

    def test_a_pass_whose_summary_line_fell_out_still_counts(self):
        takes = self.counts(ELEC_OLD)
        self.assertEqual([(t["where"], t["taken"], t["relit"], t.get("unsplit"), t.get("now_listed"))
                          for t in takes],
                         [("waves task / wave w1111-elec2028 main", 975, 0, 975, 6159),
                          ("waves task / wave w1111-pol main", 1, 1, None, 6159)])

    def test_a_pass_with_its_summary_line_is_counted_once(self):
        takes = self.counts(ELEC_NEW)
        self.assertEqual([(t["where"], t["taken"], t["relit"], t.get("unsplit"), t.get("now_listed"), t["usd_day"])
                          for t in takes],
                         [("waves task / wave w1111-elec2028 main", 975, 975, None, 6159, 600.0),
                          ("waves task / wave w1111-pol main", 1, 1, None, 6159, 2.0)])
        self.assertEqual((takes[0]["bar"], takes[0].get("sized")), (1.10, None))

    def test_the_bar_and_the_size_up_count_are_read(self):
        # the 20:02Z 11/18 FX pass, the first with the size-up to the bar
        line = ("    2026-11-18 05:07:10Z scored 201 clearing $1.10 | skipped {'no book': 0, 'no quotable side': 0, "
                "'under bar': 1} | taking 201 by ROI (extended 0, relit 0, new 201), new at risk if filled $26,665 "
                "(budget $700,000), ~$76/day; 0 over budget; 12 clear the bar only at the cheap-side tier size "
                "(tiers 2:10,5:5); 189 only at the bank max 300 x up to 2 (x1 87, x1.25 56, x1.5 43, x1.75 2, x2 1)\n")
        t = self.counts("== wave w1113-fxmetals main 2026-11-18T05:00:21Z: scan (write)\n" + line)[0]
        self.assertEqual((t["taken"], t["skipped"]["under bar"], t["bar"], t["sized"], t["bank_max"], t["sized_cap"]),
                         (201, 1, 1.10, 189, 300, 2.0))


if __name__ == "__main__":
    unittest.main()
