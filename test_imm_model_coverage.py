#!/usr/bin/env python3
"""Unit tests for imm_model_coverage.py (Jack 2026-10-09: "ensure i get
alerted" when a new market in a per-entity-modeled family is not modeled).

Run: python -m unittest test_imm_model_coverage
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_model_coverage as mc                                      # noqa: E402
import incentive_mm as imm                                           # noqa: E402


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


class TestCoverage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="imm_cov_test_")
        self.now = time.time()
        self.rain = os.path.join(self.dir, "rain_fair_values.json")
        self.ca = os.path.join(self.dir, "carbon_arc_fair.json")
        p = [mock.patch.object(imm, "RAIN_FAIR_FILE", self.rain),
             mock.patch.object(imm, "CA_FAIR_FILE", self.ca)]
        for x in p:
            x.start()
            self.addCleanup(x.stop)
        iso = datetime.now(timezone.utc).isoformat()
        _write(self.rain, {"generated_at": iso, "auto_stations": {"BNA": {}},
                           "unmapped": {"ZZZ": "CLIZZZ: no NWS station for CLIZZZ"}})
        _write(self.ca, {"generated_at": iso, "series_map": {"KXTGTFT": {}}})

    def test_an_unmodeled_rain_city_is_a_gap_with_its_reason(self):
        progs = {t: {} for t in ("KXRAIN-26OCT12-NYC", "KXRAIN-26OCT12-BNA",
                                 "KXRAIN-26OCT12-ZZZ", "KXRAIN-26OCT13-ZZZ",
                                 "KXRAINNYCM-26OCT-4")}
        gaps = mc.find_gaps(progs, self.now, [mc.check_rain_daily])
        self.assertEqual(list(gaps), ["Daily rain (rain_fair)|ZZZ"])
        g = gaps["Daily rain (rain_fair)|ZZZ"]
        self.assertEqual(g["markets"], ["KXRAIN-26OCT12-ZZZ", "KXRAIN-26OCT13-ZZZ"])
        self.assertEqual(g["fail"], "open")
        self.assertIn("CLIZZZ", g["why"])

    def test_an_unmapped_carbon_arc_series_is_a_gap(self):
        progs = {"KXTGTFT-26NOV08-T90": {}, "KXDPZFT-26NOV08-T50": {}, "KXGOOD-99DEC31-A": {}}
        verdict = {"KXTGTFT": True, "KXDPZFT": True}.get
        with mock.patch.object(imm, "family_verdict", verdict):
            gaps = mc.find_gaps(progs, self.now, [mc.check_carbon_arc])
        self.assertEqual(list(gaps), ["Carbon Arc (carbon_arc_fair)|KXDPZFT"])

    def test_a_stale_fair_file_and_a_failing_check_are_gaps_too(self):
        _write(self.rain, {"generated_at": "2026-01-01T00:00:00+00:00"})
        progs = {"KXRAIN-26OCT12-NYC": {}}

        def broken(_progs, _now):
            raise ValueError("bad file")
        gaps = mc.find_gaps(progs, self.now, [mc.check_rain_daily, broken])
        self.assertIn("Daily rain (rain_fair)|(fair file)", gaps)
        self.assertIn("broken|(check failed)", gaps)

    def test_treasury_vercel_and_youtube_gaps(self):
        import treasury_fair as tf
        import vercel_fair as vf
        progs = {"KXNEWTSY-26OCT30-T4.5": {}, "KXUST5AM-26OCT30-T5.17": {},
                 "KXNEWVREQ-10OCT26-T5": {}, "KXYTVIEWSW-ARI26OCT11-13.0M": {},
                 "KXYTVIEWSW-KAT26OCT11-20.0M": {}}
        with mock.patch.object(imm, "treasury_gated_series",
                               lambda s: s in ("KXNEWTSY", "KXUST5AM")), \
                mock.patch.object(tf, "classify",
                                  lambda s: None if s == "KXNEWTSY" else {"inst": "x"}), \
                mock.patch.object(tf, "strike_of", lambda t: 5.17), \
                mock.patch.object(imm, "vercel_series", lambda s: s == "KXNEWVREQ"), \
                mock.patch.object(imm, "ytw_series", lambda s: s == "KXYTVIEWSW"), \
                mock.patch.object(imm, "YTW_ARTISTS", frozenset({"KAT"})):
            gaps = mc.find_gaps(progs, self.now, [mc.check_treasury, mc.check_vercel,
                                                  mc.check_youtube_weekly])
        self.assertEqual(sorted(gaps), [
            "Treasury yields (treasury_fair)|KXNEWTSY: no Treasury fair model for this series",
            "Vercel labs (vercel_fair)|KXNEWVREQ: no lab for this series in vercel_fair.LAB_SERIES",
            "YouTube weekly pilot (yt_weekly_fair)|ARI"])
        self.assertTrue(gaps["YouTube weekly pilot (yt_weekly_fair)|ARI"]["once"])

    def test_datacenter_and_unmodelled_sports_ladders(self):
        dc = os.path.join(self.dir, "datacenter_fair.json")
        _write(dc, {"updated_at": datetime.now(timezone.utc).isoformat(),
                    "states": {"TX": {"count": 5}}})
        progs = {"KXTXDATACENTERS-26-T5": {}, "KXNVDATACENTERS-26-T3": {},
                 "KXNBALADDERPTS-26OCT25LALGSW-X1": {}, "KXNFLLADDERREC-26OCT11DETARI-Y1": {}}
        lg = {"KXNBALADDERPTS": "NBA", "KXNFLLADDERREC": "NFL"}.get
        with mock.patch.object(imm, "DC_FAIR_FILE", dc), \
                mock.patch.object(imm, "dc_state_of",
                                  {"KXTXDATACENTERS": "TX", "KXNVDATACENTERS": "NV"}.get), \
                mock.patch.object(imm, "sports_ladder_league", lg), \
                mock.patch.object(mc, "_allowed", lambda t: True):
            gaps = mc.find_gaps(progs, self.now, [mc.check_datacenter,
                                                  mc.check_sports_ladders])
        self.assertEqual(sorted(gaps), ["Data-center counts (datacenter_fair)|NV",
                                        "Sports ladders without a model|NBA KXNBALADDERPTS"])
        self.assertEqual(gaps["Sports ladders without a model|NBA KXNBALADDERPTS"]["fail"], "open")

    def test_a_new_vercel_lab_is_reported_once(self):
        import vercel_fair as vf
        d = os.path.join(self.dir, "vercel")
        os.makedirs(d)
        with open(os.path.join(d, "export_2026-10-10.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"labs": {"2026-10-10": {
                "deepseek|tokens": 9.0, "anthropic|tokens": 20.0, "newlab|tokens": 1.25}}}) + "\n")
        progs = {f"{vf.OPEN_SERIES}-26OCT13-T64.7": {}}
        with mock.patch.object(vf, "INTRADAY_DIR", d):
            gaps = mc.find_gaps(progs, self.now, [mc.check_vercel_labs])
        self.assertEqual(list(gaps), ["Vercel open-weight labs (vercel_fair)|newlab"])
        self.assertIn("1.25% of tokens", gaps["Vercel open-weight labs (vercel_fair)|newlab"]["why"])

    def test_by_design_gaps_are_baselined_on_day_one_and_remembered(self):
        once = mc._gap("YT", "ARI", ["X-1"], "closed", "pilot", "admit")
        once["once"] = True
        real = mc._gap("Rain", "BNA", ["KXRAIN-1-BNA"], "open", "no station", "add")
        d1, st = mc.due({}, {"YT|ARI": once, "Rain|BNA": real}, 1000.0, baseline_once=True)
        self.assertEqual([k for k, _g, _f, _n in d1], ["Rain|BNA"])      # only the real one
        d2, st = mc.due(st, {}, 2000.0)                                   # both close
        self.assertEqual(set(st), {"YT|ARI"})                             # the exclusion is remembered
        d3, st = mc.due(st, {"YT|ARI": once}, 3000.0 + 7 * 86400)         # next week's event: quiet
        self.assertEqual(d3, [])
        d4, st = mc.due(st, {}, 3000.0 + 40 * 86400)                      # forgotten after 30 days
        self.assertEqual(st, {})

    def test_alert_on_first_sight_then_daily_while_open(self):
        gaps = {"F|A": mc._gap("F", "A", ["X-1"], "open", "why", "fix")}
        d1, st = mc.due({}, gaps, 1000.0)
        self.assertEqual([(k, new) for k, _g, _f, new in d1], [("F|A", True)])
        d2, st = mc.due(st, gaps, 1000.0 + 3600)             # an hour later: quiet
        self.assertEqual(d2, [])
        d3, st = mc.due(st, gaps, 1000.0 + mc.REALERT_HOURS * 3600 + 1)
        self.assertEqual([(k, new) for k, _g, _f, new in d3], [("F|A", False)])
        d4, st = mc.due(st, {}, 1000.0 + 2 * mc.REALERT_HOURS * 3600)
        self.assertEqual((d4, st), ([], {}))                  # closed: forgotten
        subject, text = mc.body(d1)
        self.assertIn("1 gap(s), 1 market(s), 1 new", subject)
        self.assertIn("FAIL-OPEN", text)
        text.encode("ascii")                                  # cp1252 task console


if __name__ == "__main__":
    unittest.main()
