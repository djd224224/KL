"""The new-series pass enrolls a Kalshi Elections series even when a wave list
admitted its markets first (2026-11-11, w1111-statenom: KXSENATENOM, KXGOVNOM and
7 state-race series were listed by the 24/7 wave check before the family watch
saw them, so `_allowed` skipped them and 231 listed long shots sat behind the
bot's entry screen). No network, and every file the pass writes goes to a temp
dir: STATUS_DIR and the import-time file paths are pointed there first."""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import incentive_mm as imm
import imm_earnings_overrides as ieo

FAR = datetime.now(timezone.utc) + timedelta(days=30)
PAST = datetime.now(timezone.utc) - timedelta(hours=1)


class WaveListedElectionsEnroll(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="enroll-wave-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for obj, name, val in (
                (imm, "STATUS_DIR", self.tmp),
                (imm, "EXTRA_ALLOW_FILE", os.path.join(self.tmp, "extra_allow_series.json")),
                (imm, "FINECON_EXTRA_FILE", os.path.join(self.tmp, "finecon_extra_series.json")),
                (imm, "ALLOWLIST_ONLY", True),
                (imm, "PVD_ENABLE", True)):
            p = mock.patch.object(obj, name, val)
            p.start()
            self.addCleanup(p.stop)
        if hasattr(ieo, "EXTRA_ALLOW_FILE"):
            p = mock.patch.object(ieo, "EXTRA_ALLOW_FILE", os.path.join(self.tmp, "extra_allow_series.json"))
            p.start()
            self.addCleanup(p.stop)
        # nothing below may touch the live run-logs
        self.assertTrue(imm.election_extra_path().startswith(self.tmp))
        saved = set(imm.ELECTION_EXTRA_SERIES)
        self.addCleanup(lambda: (imm.ELECTION_EXTRA_SERIES.clear(), imm.ELECTION_EXTRA_SERIES.update(saved),
                                 imm._election_extra_state.update(mtime=0.0)))
        imm._election_extra_state["mtime"] = 0.0
        self.listed = {
            "KXSTATERACEZZ-28XX-ABC": {"stop": FAR},          # Elections, list only
            "KXLISTEDPOLZZ-28-ABC": {"stop": FAR},            # Politics, list only
            "KXSERBIAELECTIONCALL-26NOV01-26OCT15": {"stop": FAR},   # an Elections carve-out
            "KXGONESTATEZZ-28XX-ABC": {"stop": PAST},         # its list stop has passed
        }
        p = mock.patch.dict(imm.PVD_MARKETS, self.listed)
        p.start()
        self.addCleanup(p.stop)

    def client(self, reads):
        cats = {"KXSTATERACEZZ": "Elections", "KXLISTEDPOLZZ": "Politics",
                "KXSERBIAELECTIONCALL": "Elections", "KXFOOELECTIONZZ": "Elections",
                "KXGONESTATEZZ": "Elections"}
        tickers = list(self.listed) + ["KXFOOELECTIONZZ-26DEC06-A"]

        class _Client:
            def get(self, path, params=None):
                if path == "/incentive_programs":
                    return {"incentive_programs": [{"market_ticker": t} for t in tickers]}
                if path.startswith("/series/"):
                    reads.append(path[8:])
                    return {"series": {"category": cats.get(path[8:], "")}}
                raise AssertionError(path)
        return _Client()

    def test_the_list_only_test(self):
        self.assertTrue(ieo.allowed_by_wave_list_only("KXSTATERACEZZ-28XX-ABC"))
        self.assertTrue(imm.PVD_ENABLE)                               # switched back
        self.assertFalse(ieo.allowed_by_wave_list_only("KXGONESTATEZZ-28XX-ABC"))   # stop passed
        self.assertFalse(ieo.allowed_by_wave_list_only("KXUNLISTEDZZ-28-A"))       # not listed
        # listed AND a family member: covered by the family, not the list alone
        with mock.patch.dict(imm.PVD_MARKETS, {"KXDENVERMAYOR-27-ABC": {"stop": FAR}}):
            self.assertTrue(imm.IncentiveMarketMaker._allowed("KXDENVERMAYOR-27-ABC"))
            self.assertFalse(ieo.allowed_by_wave_list_only("KXDENVERMAYOR-27-ABC"))

    def test_a_listed_elections_series_joins_the_family(self):
        reads = []
        with mock.patch.object(ieo, "log", lambda *a, **k: None):
            enrolled, review = ieo.enroll_new_series(self.client(reads), dry=False)
        got = {s: why for s, why, _x in enrolled}
        self.assertEqual(got.get("KXSTATERACEZZ"), ieo.ELECTION_LISTED_ENROLL_WHY)
        self.assertEqual(got.get("KXFOOELECTIONZZ"), ieo.ELECTION_ENROLL_WHY)   # the old path, unchanged
        self.assertEqual(got.get("KXGONESTATEZZ"), ieo.ELECTION_ENROLL_WHY)     # off the list: the old path
        self.assertNotIn("KXLISTEDPOLZZ", got)                  # another category: left as it was
        self.assertNotIn("KXSERBIAELECTIONCALL", got)           # carve-outs never join
        rev = {s for s, _w, _x in review}
        self.assertNotIn("KXLISTEDPOLZZ", rev)                  # ...and no review row for it
        self.assertNotIn("KXSTATERACEZZ", rev)
        self.assertNotIn("KXSERBIAELECTIONCALL", reads)         # excluded before any read
        with open(imm.election_extra_path(), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["series"],
                             ["KXFOOELECTIONZZ", "KXGONESTATEZZ", "KXSTATERACEZZ"])
        self.assertFalse(os.path.exists(imm.EXTRA_ALLOW_FILE))
        # the bot reads it as a member; the next run neither re-reads nor re-enrolls it
        imm.load_election_extra_series()
        self.assertTrue(imm.election_series("KXSTATERACEZZ"))
        self.assertFalse(ieo.allowed_by_wave_list_only("KXSTATERACEZZ-28XX-ABC"))
        reads2 = []
        with mock.patch.object(ieo, "log", lambda *a, **k: None):
            enrolled2, _ = ieo.enroll_new_series(self.client(reads2), dry=False)
        self.assertEqual(enrolled2, [])
        self.assertEqual(reads2, ["KXLISTEDPOLZZ"])             # the one list-only series left

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(ieo, "log", lambda *a, **k: None):
            enrolled, _ = ieo.enroll_new_series(self.client([]), dry=True)
        self.assertIn("KXSTATERACEZZ", {s for s, _w, _x in enrolled})
        self.assertFalse(os.path.exists(imm.election_extra_path()))


if __name__ == "__main__":
    unittest.main()
