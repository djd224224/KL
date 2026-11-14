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


if __name__ == "__main__":
    unittest.main()
