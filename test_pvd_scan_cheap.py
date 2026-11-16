"""pvd_scan leaves the cheap-side books' markets alone (Jack 2026-11-16: "Yes, move
them", to "Move the 154 primaries off the 2028 wave onto the cheap-side books?"): the
bot lets a wave entry win over a cheap-side entry, so a market on the cheap-side list
is never listed by a wave scan. No network."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

# test_pvd_waves puts stand-ins for incentive_mm / kalshi_reads in sys.modules: import
# the real ones here, and put the stand-ins back after (as test_pvd_scan_tiers does)
_stubs = {m: sys.modules.pop(m) for m in ("incentive_mm", "kalshi_reads")
          if m in sys.modules and not getattr(sys.modules[m], "__file__", None)}
import pvd_scan as ps  # noqa: E402
sys.modules.update(_stubs)


class CheapListed(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pvd_cheap_")
        self.path = os.path.join(self.dir, "cheap_side_books.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, body):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(body if isinstance(body, str) else json.dumps(body))

    def test_every_listed_market_live_or_past_its_stop(self):
        self._write({"last_write": "x", "markets": {
            "KXDPRESPRIMARY-28AK-JTAL": {"side": "yes", "px": 2, "size": 4000,
                                         "stop": "2026-11-18T05:00:00Z"},
            "KXPRESSTATE-AR-28-D": {"side": "yes", "px": 2, "size": 4000,
                                    "stop": "2020-01-01T00:00:00Z"}}})
        self.assertEqual(ps.cheap_listed(self.path),
                         {"KXDPRESPRIMARY-28AK-JTAL", "KXPRESSTATE-AR-28-D"})

    def test_missing_or_unreadable_lists_as_before(self):
        self.assertEqual(ps.cheap_listed(self.path), set())        # no file
        self._write("{not json")
        self.assertEqual(ps.cheap_listed(self.path), set())
        self._write({"markets": None})
        self.assertEqual(ps.cheap_listed(self.path), set())
        self._write([1, 2])
        self.assertEqual(ps.cheap_listed(self.path), set())

    def test_off(self):
        self._write({"markets": {"KXDPRESPRIMARY-28AK-JTAL": {}}})
        with mock.patch.object(ps, "CHEAP_SKIP", False):
            self.assertEqual(ps.cheap_listed(self.path), set())

    def test_the_default_path_is_the_bots_cheap_list(self):
        self.assertEqual(os.path.basename(ps.CHEAP_LIST), "cheap_side_books.json")
        self.assertEqual(os.path.dirname(ps.CHEAP_LIST), os.path.dirname(ps.LIST))
        self.assertTrue(ps.CHEAP_SKIP)


if __name__ == "__main__":
    unittest.main()
