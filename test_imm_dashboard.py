"""Tests for imm_dashboard.py -- the numbers, not the page.

The integration test builds a synthetic run-logs directory for one ET day and
checks every headline figure against hand arithmetic: reward accrual from the
cycle log, mark-to-market trading P&L across a window (realized + change in
unrealized), a settlement, a manual offset treated as a transfer, and a fill's
30-minute mark-out."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone

import imm_dashboard as dash


def _ts(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


class FamilyTests(unittest.TestCase):
    def test_mentions(self):
        self.assertEqual(dash.family_of("KXEARNINGSMENTIONCCL"), ("Earnings mentions", "CCL"))
        self.assertEqual(dash.family_of("KXTRUMPMENTIONB")[1], "TRUMPMENTION-B")
        self.assertEqual(dash.family_of("KXTRUMPMENTION")[0], "TRUMP mentions")
        self.assertEqual(dash.family_of("KXWORLDNEWSMENTION")[0], "Other mentions")

    def test_approval_is_not_a_mention(self):
        # the rewards report files KXTRUMPAPPROVE under "TRUMP mention"; for
        # triage it is a polling-average market and belongs with politics
        self.assertEqual(dash.family_of("KXTRUMPAPPROVE")[0], "Politics & approval")

    def test_elections(self):
        self.assertEqual(dash.family_of("KXBEXARCOUNTYJUDGE"), ("Elections", "County judges"))
        self.assertEqual(dash.family_of("KXLEXMAYOR"), ("Elections", "Mayors"))
        self.assertEqual(dash.family_of("KXHOUSEWINSTATE"), ("Elections", "US House"))
        self.assertEqual(dash.family_of("KXATTYGENTX"), ("Elections", "Attorneys general"))
        self.assertEqual(dash.family_of("KXSAARLAND"), ("Elections", "General elections"))
        # carve-outs stay out of the family
        self.assertNotEqual(dash.family_of("KXISTANBULMAYOR")[0], "Elections")

    def test_carbon_arc_vs_food_trackers(self):
        self.assertEqual(dash.family_of("KXAMZNCC"), ("Carbon Arc consumer", "*CC card spend"))
        self.assertEqual(dash.family_of("KXGEMINIAPP")[1], "*APP app charts")
        self.assertEqual(dash.family_of("KXCAVAFT")[1], "*FT foot traffic")
        # KXCOCACOLAPOS / KXYUMTBFT are price trackers despite the suffix
        self.assertEqual(dash.family_of("KXCOCACOLAPOS"), ("Company KPIs", "Food price trackers"))
        self.assertEqual(dash.family_of("KXYUMTBFT"), ("Company KPIs", "Food price trackers"))

    def test_other_families(self):
        self.assertEqual(dash.family_of("KXRT"), ("Rotten Tomatoes", "KXRT"))
        self.assertEqual(dash.family_of("KXNFLESCALATORRECYDS"), ("Sports & awards", "Sports ladders"))
        self.assertEqual(dash.family_of("KXVENUEPERFORM")[0], "Sports & awards")
        self.assertEqual(dash.family_of("KXAAAGASD"), ("Gas & diesel", "AAA national daily"))
        self.assertEqual(dash.family_of("KXAAAGASDTX"), ("Gas & diesel", "AAA state dailies"))
        self.assertEqual(dash.family_of("KXDIESELW"), ("Gas & diesel", "Diesel weekly"))
        self.assertEqual(dash.family_of("KXRAIN"), ("Weather & quakes", "Rain dailies"))
        self.assertEqual(dash.family_of("KXCPICORE"), ("Econ & rates", "CPI & inflation"))
        self.assertEqual(dash.family_of("KXUST10AM"), ("Econ & rates", "Treasury yields"))
        self.assertEqual(dash.family_of("KXBABELMANDEBWEEKLY")[0], "Commodities & shipping")
        self.assertEqual(dash.family_of("KXBTCVSGOLD")[0], "Crypto")
        self.assertEqual(dash.family_of("KXBA")[0], "Company KPIs")
        self.assertEqual(dash.family_of("KXZZZUNKNOWN"), ("Other prints", "KXZZZUNKNOWN"))

    def test_audit_misfits(self):
        # found by the 9/29 audit: these were filed under the wrong family
        self.assertEqual(dash.family_of("KXTEMPHELP"), ("Econ & rates", "Jobs & economy"))
        self.assertEqual(dash.family_of("KXNECOF"), ("Commodities & shipping", "Agriculture"))
        self.assertEqual(dash.family_of("KXVSXY")[0], "Company KPIs")
        self.assertEqual(dash.family_of("KXCTCA")[0], "Company KPIs")
        self.assertEqual(dash.family_of("KXPADATACENTERS")[0], "AI & tech")
        self.assertEqual(dash.family_of("KXCASESSION")[0], "Politics & approval")
        self.assertEqual(dash.family_of("KXSAMOMINF"), ("Econ & rates", "CPI & inflation"))
        # hourly temperature keeps its family
        self.assertEqual(dash.family_of("KXTEMPNYCH"), ("Weather & quakes", "Hourly temp"))

    def test_kalshi_category_places_unknown_series(self):
        self.assertEqual(dash.family_of("KXZZZUNKNOWN", "Economics")[0], "Econ & rates")
        self.assertEqual(dash.family_of("KXZZZUNKNOWN", "Science and Technology")[0], "AI & tech")
        # a rule always beats the category
        self.assertEqual(dash.family_of("KXRAIN", "Economics")[0], "Weather & quakes")


class WindowTests(unittest.TestCase):
    def test_et_days_and_roll(self):
        now = _ts("2026-09-28T20:00:00Z")          # 4 PM EDT
        w = dash.build_windows(now)
        self.assertEqual(w["today"]["start"], _ts("2026-09-28T04:00:00Z"))
        self.assertEqual(w["yesterday"]["start"], _ts("2026-09-27T04:00:00Z"))
        self.assertEqual(w["yesterday"]["end"], w["today"]["start"])
        self.assertEqual(w["7d"]["start"], _ts("2026-09-22T04:00:00Z"))
        self.assertEqual(w["roll"]["start"], _ts("2026-09-28T10:00:00Z"))   # 5am CDT
        self.assertEqual(w["24h"]["start"], _ts("2026-09-27T20:00:00Z"))

    def test_roll_before_5am_ct_is_previous_day(self):
        now = _ts("2026-09-28T08:00:00Z")          # 3 AM CDT
        self.assertEqual(dash.bot_roll_start(now), _ts("2026-09-27T10:00:00Z"))

    def test_dst_fall_back(self):
        # 2026-11-01 is 25 hours long in ET; midnight is 04:00Z before and 05:00Z after
        now = _ts("2026-11-02T15:00:00Z")
        w = dash.build_windows(now)
        self.assertEqual(w["today"]["start"], _ts("2026-11-02T05:00:00Z"))
        self.assertEqual(w["yesterday"]["start"], _ts("2026-11-01T04:00:00Z"))


class CycleParseTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, rows):
        path = os.path.join(self.dir, "cycle_log_2026-09-28.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(",".join(dash._CYCLE_FIELDS) + "\n")
            for r in rows:
                f.write(",".join(str(x) for x in r) + "\n")
        return path

    @staticmethod
    def _row(ts, t, frac, pool, bct=0, act=0, btop="", atop=""):
        r = [ts, t, 40, 44, 1000, 1000, 1000, frac, 2, 0, 0, pool, 0, bct, act, btop, atop,
             0, 0, 0, 0, 0, 1.0, 2, 0, 0, 0, 0.5, 0, 1, 0, 0, "run1", "cfg"]
        return r

    def test_accrual_matches_the_recon_rule(self):
        path = self._write([
            self._row("2026-09-28T04:00:00Z", "KXA-1", 0.1, 864, 10, 0, 40, ""),
            self._row("2026-09-28T04:00:30Z", "KXA-1", 0.1, 864, 10, 0, 40, ""),
            self._row("2026-09-28T04:01:00Z", "KXA-1", 0.2, 864, 10, 5, 40, 45),
            # a 2-hour outage: the gap is capped at MAX_DT
            self._row("2026-09-28T06:01:00Z", "KXA-1", 0.1, 864, 10, 0, 40, ""),
        ])
        out = dash.parse_cycle_file(path, keep_last=True)
        h4 = int(_ts("2026-09-28T04:00:00Z") // 3600)
        h6 = int(_ts("2026-09-28T06:00:00Z") // 3600)
        # cycle 2: 0.1 * 86.4/day over 30s; cycle 3: 0.2 * 86.4/day over 30s
        self.assertAlmostEqual(out["hourly"]["KXA-1"][h4], 0.1 * 864 * 30 / 86400 + 0.2 * 864 * 30 / 86400)
        self.assertAlmostEqual(out["hourly"]["KXA-1"][h6], 0.1 * 864 * dash.MAX_DT / 86400)
        # resting dollars: 10 bid @ 40c = $4; plus 5 ask @ 45 (a NO bid at 55c) = $2.75
        self.assertAlmostEqual(out["rest"]["KXA-1"][h4], 4.0 * 30 + 6.75 * 30)
        self.assertEqual(len(out["cycles"]), 4)
        self.assertEqual(out["last"]["KXA-1"]["_ts"], _ts("2026-09-28T06:01:00Z"))

    def test_half_written_last_cycle_falls_back(self):
        rows = []
        for t in ("KXA-1", "KXB-1", "KXC-1", "KXD-1"):
            rows.append(self._row("2026-09-28T04:00:00Z", t, 0.1, 100))
        for t in ("KXA-1", "KXB-1", "KXC-1", "KXD-1"):
            rows.append(self._row("2026-09-28T04:00:30Z", t, 0.1, 100))
        rows.append(self._row("2026-09-28T04:01:00Z", "KXA-1", 0.1, 100))   # partial
        out = dash.parse_cycle_file(self._write(rows), keep_last=True)
        self.assertEqual(len(out["last"]), 4)
        self.assertEqual(out["last"]["KXD-1"]["_ts"], _ts("2026-09-28T04:00:30Z"))


class NewEventGroupTests(unittest.TestCase):
    NOW = _ts("2026-09-28T20:00:00Z")

    def _ev(self, ev, launched_h_ago, pool=100.0, end_h=48, **kw):
        r = {"ev": ev, "ser": ev.split("-")[0], "fam": "Other prints", "grp": "x", "what": "",
             "n": 3, "pool": pool, "start": self.NOW - launched_h_ago * 3600,
             "end": self.NOW + end_h * 3600, "launched": self.NOW - launched_h_ago * 3600,
             "bot": "not allowlisted", "q": 0, "sel": 0, "est_now": 0.0,
             "series_first": self.NOW - launched_h_ago * 3600}
        r.update(kw)
        return r

    def test_left_is_pool_times_time_remaining(self):
        g = dash.new_event_groups([self._ev("KXNEW-1", 2, pool=240, end_h=12)],
                                  self.NOW - 24 * 3600, self.NOW)[0]
        self.assertAlmostEqual(g["left"], 120.0)

    def test_routine_relisting(self):
        rows = [self._ev("KXRAIN-26SEP29", 2, series_first=self.NOW - 30 * 86400)]
        g = dash.new_event_groups(rows, self.NOW - 24 * 3600, self.NOW)[0]
        self.assertTrue(g["routine"])

    def test_promising_needs_real_dollars(self):
        rows = [self._ev("KXGOOD-1", 3, g_est=4.0, g_yld=0.03, g_reason="not in allowlist"),
                self._ev("KXTINY-1", 3, g_est=0.4, g_yld=0.05, g_reason="not in allowlist"),
                self._ev("KXHALF-1", 3, g_est=1.6, g_yld=0.025, g_reason="not in allowlist")]
        flags = {g["ser"]: g["flag"] for g in dash.new_event_groups(rows, self.NOW - 86400, self.NOW)}
        self.assertEqual(flags["KXGOOD"], "promising")
        self.assertEqual(flags["KXTINY"], "")                # high ROI on a $0.40 book
        self.assertEqual(flags["KXHALF"], "promising")       # half the bar at >= 2%/day

    def test_unestimated_pool_and_exclusions(self):
        rows = [self._ev("KXBIG-1", 3, pool=500, end_h=48),                   # $1,000 left
                self._ev("KXFX15M-1", 1, pool=480, end_h=0.2,
                         g_reason="excluded family (open-scan)"),
                self._ev("KXBLK-1", 1, pool=5000, bot="blocked (config)"),
                self._ev("KXQUO-1", 1, pool=5000, q=3, sel=3)]
        flags = {g["ser"]: g["flag"] for g in dash.new_event_groups(rows, self.NOW - 86400, self.NOW)}
        self.assertEqual(flags["KXBIG"], "unestimated pool")
        self.assertEqual(flags["KXFX15M"], "")               # deliberate + 12 min left
        self.assertEqual(flags["KXBLK"], "")                 # launcher blocklist
        self.assertEqual(flags["KXQUO"], "")                 # already quoting

    def test_window_cut_and_expired(self):
        rows = [self._ev("KXOLD-1", 30), self._ev("KXDONE-1", 1, end_h=-1)]
        self.assertEqual(dash.new_event_groups(rows, self.NOW - 24 * 3600, self.NOW), [])


class FillsDedupeTests(unittest.TestCase):
    def test_duplicate_fill_ids_count_once(self):
        d = tempfile.mkdtemp()
        old = dash.STATUS_DIR
        try:
            dash.STATUS_DIR = d
            row = {"ts": 1790640067, "fill_id": "abc", "ticker": "KXA-1", "our_book_side": "bid",
                   "yes_price_cents": 50, "count": 3, "pos_before": 0, "pos_after": 3}
            for day in ("2026-09-28", "2026-09-29"):          # written into both day files
                with open(os.path.join(d, f"fills_{day}.jsonl"), "w", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
            self.assertEqual(len(dash.load_fills("2026-09-01")), 1)
        finally:
            dash.STATUS_DIR = old
            shutil.rmtree(d, ignore_errors=True)


class RenderTests(unittest.TestCase):
    def test_data_cannot_close_the_script_tag(self):
        page = dash.render({"x": "</script><script>alert(1)</script>", "timing": {}})
        body = page.split("const D = ", 1)[1]
        self.assertNotIn("</script><script>alert", body.split("\n", 1)[0])
        self.assertIn("<\\/script>", body.split("\n", 1)[0])


class BuilderIntegrationTests(unittest.TestCase):
    """One synthetic ET day through the whole Builder."""

    NOW = _ts("2026-09-28T20:00:00Z")

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self._old = (dash.STATUS_DIR, dash.DASH_DIR, dash.CACHE_DIR)
        dash.STATUS_DIR = self.dir
        dash.DASH_DIR = os.path.join(self.dir, "dashboard")
        dash.CACHE_DIR = os.path.join(dash.DASH_DIR, "cache")
        d = "2026-09-28"
        cyc = []
        for i, ts in enumerate(("2026-09-28T04:00:00Z", "2026-09-28T04:00:30Z", "2026-09-28T04:01:00Z")):
            cyc.append(CycleParseTests._row(ts, "KXA-1", 0.1, 864, 10, 10, 50, 52))
        with open(os.path.join(self.dir, f"cycle_log_{d}.csv"), "w", encoding="utf-8", newline="") as f:
            f.write(",".join(dash._CYCLE_FIELDS) + "\n")
            for r in cyc:
                f.write(",".join(str(x) for x in r) + "\n")

        def mark(ts, t, pos, avg, mk):
            return {"ts": ts, "ticker": t, "series": t.split("-")[0], "event_ticker": t.rsplit("-", 1)[0],
                    "pos": pos, "avg_cents": avg, "mark_cents": mk, "realized_dollars": 0.0}
        marks = [
            mark("2026-09-28T03:58:00+00:00", "KXA-1", 10, 40, 50),
            mark("2026-09-28T04:03:00+00:00", "KXA-1", 10, 40, 55),
            mark("2026-09-28T04:03:00+00:00", "KXB-1", 5, 20, 30),     # later offset manually
            mark("2026-09-28T04:03:00+00:00", "KXC-1", -4, 30, 50),    # later settles YES
            mark("2026-09-28T04:03:00+00:00", "KXV-1", -40, 56, 2),    # vanishes at 12:02, no record
            mark("2026-09-28T10:33:00+00:00", "KXA-1", 15, 43.33, 45),
            mark("2026-09-28T10:33:00+00:00", "KXB-1", 5, 20, 31),
            mark("2026-09-28T10:33:00+00:00", "KXC-1", -4, 30, 55),
            mark("2026-09-28T10:33:00+00:00", "KXV-1", -40, 56, 2),
            mark("2026-09-28T10:33:00+00:00", "KXP-1", 7, 10, 12),     # a blip: gone 12:02, back 14:00
            mark("2026-09-28T12:02:00+00:00", "KXA-1", 15, 43.33, 50),
            mark("2026-09-28T12:02:00+00:00", "KXC-1", -4, 30, 60),
            mark("2026-09-28T14:00:00+00:00", "KXA-1", 15, 43.33, 52),
            mark("2026-09-28T14:00:00+00:00", "KXC-1", -4, 30, 62),
            mark("2026-09-28T14:00:00+00:00", "KXP-1", 7, 10, 13),
            mark("2026-09-28T19:58:00+00:00", "KXA-1", 15, 43.33, 60),
            mark("2026-09-28T19:58:00+00:00", "KXP-1", 7, 10, 14),
        ]
        self._jsonl(f"marks_{d}.jsonl", marks)
        self._jsonl(f"realized_{d}.jsonl", [
            {"ts": "2026-09-28T15:00:05+00:00", "ticker": "KXC-1", "event_ticker": "KXC",
             "realized_delta_dollars": -2.8},
        ])
        self._jsonl(f"settlements_{d}.jsonl", [
            {"ts": "2026-09-28T12:00:00+00:00", "ticker": "KXB-1", "event_ticker": "KXB",
             "result": "manual_offset", "settle_price_cents": None, "own_pos_at_settle": 5,
             "own_avg_cents": 20, "market_realized_dollars": 0.0},
            {"ts": "2026-09-28T15:00:05+00:00", "ticker": "KXC-1", "event_ticker": "KXC",
             "result": "yes", "settle_price_cents": 100.0, "own_pos_at_settle": -4,
             "own_avg_cents": 30, "market_realized_dollars": -2.8},
        ])
        f1 = {"ts": _ts("2026-09-28T10:00:00Z"), "fill_id": "f1", "ticker": "KXA-1",
              "event_ticker": "KXA", "side": "yes", "action": "buy", "count": 5,
              "yes_price_cents": 50, "is_taker": False, "our_book_side": "bid", "is_pad": False,
              "pos_before": 10, "pos_after": 15}
        # the sink sometimes writes a fill twice around the UTC file roll
        self._jsonl(f"fills_{d}.jsonl", [f1, dict(f1)])
        self._jsonl(f"guard_skips_{d}.jsonl", [
            {"ts": "2026-09-28T19:00:00+00:00", "kind": "enter", "ticker": "KXA-1", "prev": None,
             "guard": "band_both_out", "inputs": {}, "run_id": "r1"},
            {"ts": "2026-09-28T19:10:00+00:00", "kind": "enter", "ticker": "KXD-1", "prev": None,
             "guard": "fill_burst", "inputs": {}, "run_id": "r1"},
            {"ts": "2026-09-28T19:20:00+00:00", "kind": "clear", "ticker": "KXA-1", "prev": "band_both_out",
             "guard": None, "run_id": "r1"},
        ])
        with open(os.path.join(self.dir, "imm_state.json"), "w", encoding="utf-8") as f:
            json.dump({"selected_tickers": ["KXA-1"], "own_pos": {"KXA-1": 15}, "own_avg": {"KXA-1": 43.33},
                       "toxic_halt_until": {"KXA-1|bid": self.NOW + 600, "KXZ-1|ask": self.NOW - 5},
                       "reward_est_today": 1.0, "pnl_today_carry": -1.0}, f)
        with open(os.path.join(self.dir, "status_incentive_mm.json"), "w", encoding="utf-8") as f:
            json.dump({"updated_at": "2026-09-28T19:59:00Z", "mode": "LIVE"}, f)

    def tearDown(self):
        dash.STATUS_DIR, dash.DASH_DIR, dash.CACHE_DIR = self._old
        shutil.rmtree(self.dir, ignore_errors=True)

    def _jsonl(self, name, rows):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_today_window(self):
        b = dash.Builder(self.NOW, api=False, api_force=False)
        m = b.build()
        a, bb, c = (m["markets"][t]["w"]["today"] for t in ("KXA-1", "KXB-1", "KXC-1"))
        # rewards: two 30s gaps at 0.1 x $864/day
        self.assertAlmostEqual(a["rew"], 2 * 0.1 * 864 * 30 / 86400, places=3)
        # KXA: start snapshot 04:03 (u = 10 x 15c), end 19:58 (u = 15 x 16.67c)
        self.assertAlmostEqual(a["pnl"], 15 * (60 - 43.33) / 100 - 10 * (55 - 40) / 100, places=3)
        # KXB left by an offset row, no API to resolve it: a transfer at its
        # last mark -- the only P&L is the mark move 04:03 -> 10:33 (30 -> 31)
        self.assertAlmostEqual(bb["pnl"], 5 * (31 - 30) / 100, places=6)
        # KXC settled YES short 4 from a 50c mark: -4 x (100 - 50) / 100
        self.assertAlmostEqual(c["pnl"], -2.0, places=6)
        self.assertEqual(c.get("settled"), "settled YES")
        # the fill's 30-min mark-out: bought 5 @ 50, mark 45 at 10:33 (the
        # duplicated sink row counts once)
        self.assertAlmostEqual(a["mk"], 5 * (45 - 50) / 100, places=6)
        self.assertEqual(a["fills"], 1)
        # the curve ends on the window total
        self.assertAlmostEqual(m["curves"]["today"][-1][2],
                               sum(v["w"].get("today", {}).get("pnl", 0) for v in m["markets"].values()),
                               places=1)

    def test_halts_and_guards(self):
        m = dash.Builder(self.NOW, api=False, api_force=False).build()
        self.assertEqual([x["t"] for x in m["halts"]["side"]], ["KXA-1"])      # expired one dropped
        self.assertEqual({g["t"] for g in m["halts"]["guards"]}, {"KXD-1"})    # KXA-1 cleared
        self.assertTrue(m["halts"]["guards"][0]["risk"])
        self.assertIn("KXA-1", m["halts"]["by_market"])

    def test_second_run_uses_nothing_stale(self):
        dash.Builder(self.NOW, api=False, api_force=False).build()
        m2 = dash.Builder(self.NOW, api=False, api_force=False).build()
        self.assertAlmostEqual(m2["markets"]["KXC-1"]["w"]["today"]["pnl"], -2.0, places=6)

    def test_rows_after_now_are_ignored(self):
        # the bot keeps writing while a (slow) build runs: a snapshot stamped
        # after the build's "now" must not push the window's end edge back to
        # an older snapshot (9/30: today read +$6.76 here vs +$71.05 in history)
        with open(os.path.join(self.dir, "marks_2026-09-28.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": "2026-09-28T20:03:00+00:00", "ticker": "KXA-1", "pos": 15,
                                "avg_cents": 43.33, "mark_cents": 90}) + "\n")
        m = dash.Builder(self.NOW, api=False, api_force=False).build()
        self.assertEqual(m["edges"]["today"][1], _ts("2026-09-28T19:58:00Z"))
        self.assertFalse(m["warnings"])
        a = m["markets"]["KXA-1"]["w"]["today"]
        self.assertAlmostEqual(a["pnl"], 15 * (60 - 43.33) / 100 - 10 * (55 - 40) / 100, places=3)
        h = {r["d"]: r for r in m["history"]}["2026-09-28"]
        self.assertAlmostEqual(h["pnl"], sum(v["w"].get("today", {}).get("pnl", 0)
                                             for v in m["markets"].values()), places=1)

    def test_every_history_day_matches_its_window(self):
        m = dash.Builder(self.NOW, api=False, api_force=False).build()
        d = m["days"]["2026-09-28"]
        f = m["day_fields"]
        self.assertTrue(d["ok"])
        for t, row in d["m"].items():
            w = m["markets"][t]["w"]["today"]
            self.assertAlmostEqual(row[f.index("pnl")], w["pnl"], places=3)
            self.assertAlmostEqual(row[f.index("rew")], w["rew"], places=3)
            self.assertEqual(row[f.index("fills")], w.get("fills", 0))

    def _exits(self, lookup):
        b = dash.Builder(self.NOW, api=lookup is not None, api_force=False)
        b.load()
        if lookup is not None:
            b.market_lookup = lambda tickers: {t: lookup[t] for t in tickers if t in lookup}
        b.build_exits()
        return b

    def test_vanished_position_is_an_exit_and_a_blip_is_not(self):
        b = self._exits(None)
        kinds = {(e["t"], e["kind"]) for e in b.exits}
        self.assertIn(("KXV-1", "vanish"), kinds)
        self.assertIn(("KXB-1", "offset"), kinds)
        self.assertNotIn("KXP-1", {e["t"] for e in b.exits})       # back 90 min later
        self.assertNotIn("KXC-1", {e["t"] for e in b.exits})       # a yes/no settlement row
        # no API: out at the last mark -- the P&L stops at the 10:33 mark
        v = [e for e in b.exits if e["t"] == "KXV-1"][0]
        self.assertFalse(v["realized"])
        self.assertAlmostEqual(v["amount"], -40 * (2 - 56) / 100, places=6)
        mk, _e = b.window_markets(*[b.windows["today"][k] for k in ("start", "end")])
        self.assertAlmostEqual(mk["KXV-1"]["pnl"], 0.0, places=6)

    def test_scalar_settlement_behind_an_offset_row_is_realized(self):
        # Kalshi: KXB-1 settled SCALAR at 12c; KXV-1 settled NO
        b = self._exits({
            "KXB-1": {"ticker": "KXB-1", "status": "finalized", "result": "scalar",
                      "settlement_value_dollars": "0.1200", "settlement_ts": "2026-09-28T11:59:00Z"},
            "KXV-1": {"ticker": "KXV-1", "status": "finalized", "result": "no",
                      "settlement_ts": "2026-09-28T11:00:00Z"}})
        ex = {e["t"]: e for e in b.exits}
        self.assertTrue(ex["KXB-1"]["realized"])
        self.assertAlmostEqual(ex["KXB-1"]["amount"], 5 * (12 - 20) / 100, places=6)
        self.assertEqual(ex["KXB-1"]["label"], "settled scalar 12.0c")
        mk, _e = b.window_markets(b.windows["today"]["start"], b.windows["today"]["end"])
        # long 5 from a 30c mark at 04:03 to a 12c settlement
        self.assertAlmostEqual(mk["KXB-1"]["pnl"], 5 * (12 - 30) / 100, places=6)
        self.assertAlmostEqual(mk["KXB-1"]["real"], 5 * (12 - 20) / 100, places=6)
        # short 40 from a 2c mark (it opened before the window at 56c) to a NO settlement
        self.assertAlmostEqual(mk["KXV-1"]["pnl"], -40 * (0 - 2) / 100 - 0.0, places=6)
        self.assertEqual(mk["KXV-1"]["settled"], "settled NO")

    def test_scalar_settlement_booked_by_the_bot_counts_once(self):
        # since 2026-09-29 the bot books a scalar settlement itself: a
        # realized delta plus a "scalar" settlements row, which is no exit
        d = "2026-09-28"

        def add(name, rows):
            with open(os.path.join(self.dir, f"{name}_{d}.jsonl"), "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        for ts in ("2026-09-28T04:03:00+00:00", "2026-09-28T10:33:00+00:00"):
            add("marks", [{"ts": ts, "ticker": "KXS-1", "series": "KXS", "event_ticker": "KXS",
                           "pos": -90, "avg_cents": 16, "mark_cents": 82, "realized_dollars": 0.0}])
        add("realized", [{"ts": "2026-09-28T12:05:00+00:00", "ticker": "KXS-1", "event_ticker": "KXS",
                          "realized_delta_dollars": 3.6}])
        add("settlements", [{"ts": "2026-09-28T12:05:00+00:00", "ticker": "KXS-1", "event_ticker": "KXS",
                             "result": "scalar", "settle_price_cents": 12.0, "own_pos_at_settle": -90,
                             "own_avg_cents": 16, "market_realized_dollars": 3.6, "via": "settle_loop"}])
        b = self._exits(None)
        self.assertNotIn("KXS-1", {e["t"] for e in b.exits})
        mk, _e = b.window_markets(b.windows["today"]["start"], b.windows["today"]["end"])
        # short 90 from an 82c mark to a 12c settlement, counted once
        self.assertAlmostEqual(mk["KXS-1"]["pnl"], -90 * (12 - 82) / 100, places=6)
        self.assertAlmostEqual(mk["KXS-1"]["real"], 3.6, places=6)
        self.assertEqual(mk["KXS-1"]["settled"], "settled scalar 12.0c")


if __name__ == "__main__":
    unittest.main()
