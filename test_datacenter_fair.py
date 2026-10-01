#!/usr/bin/env python3
"""Unit tests for datacenter_fair.py -- run: python -m unittest test_datacenter_fair"""

import json
import os
import shutil
import tempfile
import unittest

import datacenter_fair as dcf

NEXT = ('<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":'
        '{"pageTitle":"Texas Data Centers - 537 Facilities from 197 Operators",'
        '"mapdata":{"geos":[{"properties":{"link":"dallas","datacenters":199}},'
        '{"properties":{"link":"austin","datacenters":66}}]},'
        '"geodata":{"meta_stats":{"dcs":{"operators":158,"mw_pipeline":42629.45}}}}}}'
        '</script>')


def page(count=537, visible=True, title=True):
    body = '<h1 class="ui header">Texas Data Centers</h1>'
    if visible:
        body += f"<p>We currently have <b>{count}</b> data centers listed, from 44 markets.</p>"
    nd = NEXT.replace("537 Facilities", f"{count} Facilities")
    if not title:
        nd = nd.replace(f"Texas Data Centers - {count} Facilities from 197 Operators", "Texas")
    return f"<html><body>{body}{nd}</body></html>"


class TestParse(unittest.TestCase):
    def test_full_page(self):
        p = dcf.parse_page(page())
        self.assertEqual((p["count"], p["title_count"], p["operators"]), (537, 537, 197))
        self.assertEqual((p["markets"], p["geo_sum"]), (2, 265))
        self.assertEqual(p["geos"], {"dallas": 199, "austin": 66})
        self.assertEqual(p["stats"], {"operators": 158, "mw_pipeline": 42629.45})

    def test_title_without_operators(self):
        html = page().replace(" from 197 Operators", "")
        self.assertEqual(dcf.parse_page(html)["operators"], None)
        self.assertEqual(dcf.parse_page(html)["count"], 537)

    def test_title_stands_in_for_the_line(self):
        self.assertEqual(dcf.parse_page(page(visible=False))["count"], 537)

    def test_no_count_raises(self):
        with self.assertRaises(ValueError):
            dcf.parse_page(page(visible=False, title=False))

    def test_slugs(self):
        self.assertEqual(dcf.STATE_SLUGS["TX"], "texas")
        self.assertEqual(dcf.STATE_SLUGS["NY"], "new-york")
        self.assertNotIn("DC", dcf.STATE_SLUGS)


class TestWriteFairFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "datacenter_fair.json")
        self.counts = {"texas": 537}
        self.calls = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def fetch(self, slug):
        self.calls.append(slug)
        n = self.counts.get(slug)
        if n is None:
            raise OSError("503")
        return page(n)

    def rows(self):
        out = []
        for name in sorted(os.listdir(self.dir)):
            if name.startswith("datacenter_counts_"):
                with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                    out += [json.loads(line) for line in f]
        return out

    def entry(self, st="TX"):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)["states"][st]

    def test_change_tracking_and_log(self):
        t0 = 1790000000.0
        ok, failed, changes = dcf.write_fair_file(self.path, self.dir, ["TX"], now=t0,
                                                  fetch=self.fetch, pause=0)
        self.assertEqual((ok, failed, changes), (1, [], []))
        e = self.entry()
        self.assertEqual((e["count"], e["changed_at"], e["prev_count"]), (537, None, None))
        self.assertEqual([r["kind"] for r in self.rows()], ["read", "detail"])
        # same count, same breakdown, same day: a read row only
        dcf.write_fair_file(self.path, self.dir, ["TX"], now=t0 + 180,
                            fetch=self.fetch, pause=0)
        self.assertIsNone(self.entry()["changed_at"])
        self.assertEqual([r["kind"] for r in self.rows()], ["read", "detail", "read"])
        # a new listing: changed_at is the first read that saw it
        self.counts["texas"] = 538
        _ok, _f, changes = dcf.write_fair_file(self.path, self.dir, ["TX"], now=t0 + 360,
                                               fetch=self.fetch, pause=0)
        self.assertEqual(changes, ["TX 537 -> 538"])
        e = self.entry()
        self.assertEqual((e["count"], e["prev_count"]), (538, 537))
        self.assertEqual(e["changed_at"], dcf._iso(t0 + 360))
        self.assertEqual(self.rows()[-1]["kind"], "detail")
        # a later read keeps the change time
        dcf.write_fair_file(self.path, self.dir, ["TX"], now=t0 + 540,
                            fetch=self.fetch, pause=0)
        self.assertEqual(self.entry()["changed_at"], dcf._iso(t0 + 360))

    def test_failed_read_keeps_the_old_entry(self):
        t0 = 1790000000.0
        dcf.write_fair_file(self.path, self.dir, ["TX"], now=t0, fetch=self.fetch, pause=0)
        del self.counts["texas"]
        ok, failed, _c = dcf.write_fair_file(self.path, self.dir, ["TX"], now=t0 + 180,
                                             fetch=self.fetch, pause=0)
        self.assertEqual((ok, failed), (0, ["TX"]))
        self.assertEqual(self.entry()["read_at"], dcf._iso(t0))      # ages out in the gate
        self.assertEqual(self.rows()[-1]["kind"], "error")

    def test_state_without_a_slug_is_not_fetched(self):
        ok, failed, _c = dcf.write_fair_file(self.path, self.dir, ["DC"], now=1790000000.0,
                                             fetch=self.fetch, pause=0)
        self.assertEqual((ok, failed, self.calls), (0, ["DC"], []))


if __name__ == "__main__":
    unittest.main()
