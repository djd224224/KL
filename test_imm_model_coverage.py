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
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_model_coverage as mc                                      # noqa: E402
import incentive_mm as imm                                           # noqa: E402

# a Carbon Arc event date whose late-month stop is still ahead: a market past
# its stop is no gap (Jack 2026-11-01), so a fixed date ages out of the tests
CA_EV = (datetime.now(timezone.utc) + timedelta(days=40)).strftime("%y%b%d").upper()


def _write(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _ts(*ymdh):
    return datetime(*ymdh, tzinfo=timezone.utc).timestamp()


class TestCoverage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="imm_cov_test_")
        self.now = time.time()
        self.rain = os.path.join(self.dir, "rain_fair_values.json")
        self.ca = os.path.join(self.dir, "carbon_arc_fair.json")
        self.vercel = os.path.join(self.dir, "vercel_fair.json")       # absent: lab sum
        p = [mock.patch.object(imm, "RAIN_FAIR_FILE", self.rain),
             mock.patch.object(imm, "CA_FAIR_FILE", self.ca),
             mock.patch.object(imm, "VERCEL_FAIR_FILE", self.vercel)]
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
        progs = {f"KXTGTFT-{CA_EV}-T90": {}, f"KXDPZFT-{CA_EV}-T50": {}, "KXGOOD-99DEC31-A": {}}
        verdict = {"KXTGTFT": True, "KXDPZFT": True}.get
        with mock.patch.object(imm, "family_verdict", verdict):
            gaps = mc.find_gaps(progs, self.now, [mc.check_carbon_arc])
        self.assertEqual(list(gaps), ["Carbon Arc (carbon_arc_fair)|KXDPZFT"])

    def test_carbon_arc_markets_past_their_stop_are_no_gap(self):
        # Jack 2026-11-01: "there are no nov markets for carbon arc yet, so why
        # am i getting this email?" -- October's events, dated by their
        # November print day, kept paying after the 10/27 04:00Z stop
        progs = {"KXTGTFT-26NOV08-T90": {}, "KXDPZFT-26NOV08-T50": {}}
        verdict = {"KXTGTFT": True, "KXDPZFT": True}.get
        for now, want in ((_ts(2026, 10, 27, 3), ["Carbon Arc (carbon_arc_fair)|KXDPZFT"]),
                          (_ts(2026, 10, 27, 5), [])):
            _write(self.ca, {"generated_at": datetime.fromtimestamp(now, timezone.utc)
                             .isoformat(), "series_map": {"KXTGTFT": {}}})
            with mock.patch.object(imm, "family_verdict", verdict), \
                    mock.patch.object(imm, "CA_LATE_STOP_DAYS", 5):
                self.assertEqual(list(mc.find_gaps(progs, now, [mc.check_carbon_arc])), want)

    def test_a_stale_carbon_arc_file_says_what_is_quoted_and_why(self):
        """11/01: the CA token 401'd from 01:13Z (the account's trial ran
        out). Every market in a program was past its stop, yet the email said
        "the bot quotes it now", and its fix said to restart."""
        now = _ts(2026, 11, 1, 13)
        _write(self.ca, {"generated_at": "2026-11-01T01:11:08+00:00",
                         "series_map": {"KXTGTFT": {}, "KXAALCC": {}}})
        log = os.path.join(self.dir, "incentive-mm-2026-11-01.log")
        with open(os.path.join(self.dir, "incentive-mm-2026-10-31.log"), "w",
                  encoding="utf-8") as f:
            f.write("2026-10-31 20:47:21Z [IMM] ca-fair refresh: 98 series with a read\n")
        with open(log, "w", encoding="utf-8") as f:
            f.write("2026-11-01 04:46:53Z === incentive_mm_v1.2 run=495c5bfc ===\n"
                    "2026-11-01 04:47:18Z [IMM] ca-fair reloaded: 98 series\n"
                    "2026-11-01 04:47:31Z [IMM] ! ca-fair refresh failed: 401 Client "
                    "Error: Unauthorized for url: https://api.carbonarc.co/v2/prisms?"
                    "insight_id=245\n"
                    "2026-11-01 05:00:00Z [IMM] cycle ...\n")
        stopped = {"KXTGTFT-26NOV08-T90": {}, "KXAALCC-26NOV07-T103": {}}
        idle_key = "Carbon Arc (carbon_arc_fair)|(fair file)"
        verdict = {"KXTGTFT": True, "KXAALCC": True}.get
        with mock.patch.object(imm, "family_verdict", verdict), \
                mock.patch.object(imm, "CA_LATE_STOP_DAYS", 5), \
                mock.patch.object(imm, "STATUS_DIR", self.dir):
            gaps = mc.find_gaps(stopped, now, [mc.check_carbon_arc])
            self.assertEqual(list(gaps), [idle_key])   # the key it always had
            g = gaps[idle_key]
            self.assertTrue(g["idle"])
            self.assertEqual(g["markets"], [])
            self.assertIn("last written 11.8h ago; nothing is quoted on it now: all 2 "
                          "Carbon Arc markets still in a program are past the bot's "
                          "stop (5 days before their data month ends)", g["why"])
            self.assertIn("rejects the API token (401 Client Error: Unauthorized", g["fix"])
            self.assertIn("bot log 2026-11-01 04:47Z", g["fix"])
            self.assertNotIn("restart if needed", g["fix"])
            subject, text = mc.body([(idle_key, g, now, True)])
            self.assertIn("0 market(s)", subject)
            self.assertNotIn("markets:", text)
            # a market before its stop is quoted on the stale file: its own key,
            # so it alerts at once rather than on the idle gap's daily re-send
            live = dict(stopped, **{f"KXTGTFT-{CA_EV}-T90": {}})
            gaps = mc.find_gaps(live, now, [mc.check_carbon_arc])
            g = gaps[idle_key + "|quoted"]
            self.assertEqual(list(gaps), [idle_key + "|quoted"])
            self.assertEqual(g["markets"], [f"KXTGTFT-{CA_EV}-T90"])
            self.assertFalse(g.get("idle"))
            self.assertIn("rejects the API token", g["fix"])
            # the last refresh line a success: the thread itself may be gone
            os.remove(log)
            g = mc.find_gaps(stopped, now, [mc.check_carbon_arc])[idle_key]
            self.assertIn("restart if needed", g["fix"])

    def test_an_unmapped_sector_array_is_a_known_gap_reported_once(self):
        # Jack 2026-10-16: "mark the ARRAY series as a known coverage gap"
        progs = {f"KXRIDEARRAY-{CA_EV}-T100": {}, f"KXDPZFT-{CA_EV}-T50": {}}
        verdict = {"KXRIDEARRAY": True, "KXDPZFT": True}.get
        with mock.patch.object(imm, "family_verdict", verdict):
            gaps = mc.find_gaps(progs, self.now, [mc.check_carbon_arc])
        arr = gaps["Carbon Arc (carbon_arc_fair)|KXRIDEARRAY"]
        self.assertTrue(arr.get("once"))
        self.assertIn("shadow", arr["why"])
        self.assertFalse(gaps["Carbon Arc (carbon_arc_fair)|KXDPZFT"].get("once"))
        # already emailed once (the 10/16 08:50 alert): never again while open
        st = {"Carbon Arc (carbon_arc_fair)|KXRIDEARRAY": {"first": 1.0, "sent": 1.0}}
        due, _st = mc.due(st, {k: g for k, g in gaps.items() if "ARRAY" in k},
                          1.0 + 3 * 86400)
        self.assertEqual(due, [])

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
                "deepseek|tokens": 9.0, "anthropic|tokens": 20.0, "newlab|tokens": 0.25}}}) + "\n")
        progs = {f"{vf.OPEN_SERIES}-26OCT13-T64.7": {}}
        with mock.patch.object(vf, "INTRADAY_DIR", d), \
                mock.patch.object(mc, "_gateway_weights", side_effect=AssertionError):
            gaps = mc.find_gaps(progs, self.now, [mc.check_vercel_labs])
        self.assertEqual(list(gaps), ["Vercel open-weight labs (vercel_fair)|newlab"])
        self.assertIn("0.25% of tokens", gaps["Vercel open-weight labs (vercel_fair)|newlab"]["why"])

    def test_a_vercel_lab_past_the_share_is_reported_once_more(self):
        # Reflection (2026-10-22): baselined at 0.002%, it reached 7.4% of
        # tokens on models Vercel counts as open, and nothing re-alerted
        import vercel_fair as vf
        fam = "Vercel open-weight labs (vercel_fair)"
        d = os.path.join(self.dir, "vercel")
        os.makedirs(d)
        with open(os.path.join(d, "export_2026-10-10.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"labs": {
                "2026-10-09": {"openlab|tokens": 3.0},          # yesterday's peak counts
                "2026-10-10": {"deepseek|tokens": 9.0, "openlab|tokens": 0.1,
                               "smalllab|tokens": 0.2, "closedlab|tokens": 6.0,
                               "mixedlab|tokens": 1.0, "unlisted|tokens": 0.8}}}) + "\n")
        progs = {f"{vf.OPEN_SERIES}-26OCT13-T64.7": {}}
        gw = {"openlab": {"open": ["openlab/a", "openlab/b"], "n": 2},
              "closedlab": {"open": [], "n": 5},
              "mixedlab": {"open": ["mixedlab/x"], "n": 3}}
        with mock.patch.object(vf, "INTRADAY_DIR", d), \
                mock.patch.object(mc, "_gateway_weights", return_value=gw):
            gaps = mc.find_gaps(progs, self.now, [mc.check_vercel_labs])
        self.assertEqual(sorted(gaps), [f"{fam}|closedlab",              # all closed: quiet
                                        f"{fam}|mixedlab (>= 0.5% of tokens)",
                                        f"{fam}|openlab (>= 0.5% of tokens)",
                                        f"{fam}|smalllab",
                                        f"{fam}|unlisted (>= 0.5% of tokens)"])
        self.assertTrue(all(g["once"] for g in gaps.values()))
        g = gaps[f"{fam}|openlab (>= 0.5% of tokens)"]
        self.assertIn("3.00% of tokens (2026-10-09)", g["why"])
        self.assertIn("all 2 of its gateway models are tagged open-weights", g["why"])
        self.assertTrue(g["fix"].startswith("add it to vercel_fair.OPEN_WEIGHT_LABS"))
        self.assertIn("mixed lab", gaps[f"{fam}|mixedlab (>= 0.5% of tokens)"]["fix"])
        self.assertIn("lists no model of it", gaps[f"{fam}|unlisted (>= 0.5% of tokens)"]["why"])
        # an unreadable model list alerts unfiltered
        with mock.patch.object(vf, "INTRADAY_DIR", d), \
                mock.patch.object(mc, "_gateway_weights", return_value=None):
            gaps2 = mc.find_gaps(progs, self.now, [mc.check_vercel_labs])
        self.assertIn("unreadable", gaps2[f"{fam}|closedlab (>= 0.5% of tokens)"]["why"])
        # the first-sight report was sent long ago; the grown lab's is new
        state = {f"{fam}|openlab": {"first": self.now - 6 * 86400, "seen": self.now - 1800,
                                    "sent": self.now - 6 * 86400, "once": True}}
        out, st = mc.due(state, gaps, self.now)
        sent = [k for k, _g, _f, _n in out]
        self.assertIn(f"{fam}|openlab (>= 0.5% of tokens)", sent)
        self.assertNotIn(f"{fam}|openlab", sent)
        self.assertIn(f"{fam}|openlab", st)                               # still remembered

    def test_a_never_read_event_waits_a_pass_before_its_email(self):
        # 2026-10-22: KXTTNSNOWM-26DEC read [NEW] "never read" from the 10
        # minutes after a restart before the snow refresher had the feed's cities
        snow = os.path.join(self.dir, "snow_monthly_fair.json")
        _write(snow, {"generated_at": datetime.now(timezone.utc).isoformat(),
                      "events": {"KXBOSSNOWM-26DEC": {}}, "event_state": {}})
        progs = {"KXTTNSNOWM-26DEC-4": {}, "KXBOSSNOWM-26DEC-10": {}}
        with mock.patch.object(imm, "SNOW_MONTHLY_FILE", snow), \
                mock.patch.object(imm, "snow_monthly_series",
                                  lambda s: s in ("KXTTNSNOWM", "KXBOSSNOWM")):
            gaps = mc.find_gaps(progs, self.now, [mc.check_snow_monthly])
        key = "Monthly snow (snow_monthly_fair)|KXTTNSNOWM-26DEC"
        self.assertEqual(list(gaps), [key])
        out, st = mc.due({}, gaps, self.now)
        self.assertEqual(out, [])                                # first sight: held
        self.assertIn(key, st)
        out2, _st = mc.due(st, {}, self.now + 1800)               # read by then: no email
        self.assertEqual(out2, [])
        out3, _st = mc.due(st, gaps, self.now + 1800)             # still unread a pass later
        self.assertEqual([(k, n) for k, _g, _f, n in out3], [(key, True)])

    def test_a_new_rain_city_whose_station_failed_says_what_failed(self):
        # 2026-10-26 (Jack: "Enroll and prevent this issue in the future"): a
        # KXRAIN<CITY>M beyond the named list is a member by name, and the
        # writer's auto-mapping failure rides into the gap
        rain = os.path.join(self.dir, "rain_monthly_fair.json")
        _write(rain, {"generated_at": datetime.now(timezone.utc).isoformat(),
                      "events": {"KXRAINQQQM-26NOV": {}},
                      "event_state": {"KXRAINQQQM-26NOV": {"code": "QQQ",
                                                           "error": "no station"}},
                      "unmapped": {"QQQ": {"at": self.now, "why": "RuntimeError: KQQQ: 404"}}})
        with mock.patch.object(imm, "RAIN_MONTHLY_FILE", rain):
            gaps = mc.find_gaps({"KXRAINQQQM-26NOV-2": {}}, self.now, [mc.check_rain_monthly])
        g = gaps["Monthly / period rain (rain_monthly_fair)|KXRAINQQQM-26NOV"]
        self.assertIn("auto-mapping failed: RuntimeError: KQQQ: 404", g["why"])

    def test_vercel_labs_are_quiet_while_the_fair_reads_the_chart(self):
        # 2026-10-22 (the meta gap): KXOPENSOURCESHARE priced off Vercel's
        # weight-class chart has no lab list to keep; on the lab sum it reports
        import vercel_fair as vf
        d = os.path.join(self.dir, "vercel")
        os.makedirs(d)
        with open(os.path.join(d, "export_2026-10-22.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"labs": {"2026-10-22": {
                "deepseek|tokens": 31.0, "meta|tokens": 0.86}}}) + "\n")
        progs = {f"{vf.OPEN_SERIES}-26OCT25-T69.1": {}}
        gw = {"meta": {"open": ["meta/llama-3.3-70b"], "n": 14}}
        for src, want in (("chart", []), ("labs (chart unreadable: Timeout)",
                                          ["Vercel open-weight labs (vercel_fair)|meta "
                                           "(>= 0.5% of tokens)"])):
            _write(self.vercel, {"anchor": {"mode": "run", "open_source": src}})
            with mock.patch.object(vf, "INTRADAY_DIR", d), \
                    mock.patch.object(mc, "_gateway_weights", return_value=gw):
                gaps = mc.find_gaps(progs, self.now, [mc.check_vercel_labs])
            self.assertEqual(sorted(gaps), want, src)

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
