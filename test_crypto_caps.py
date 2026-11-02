"""Tests for crypto_caps (the daily digest's cap check, 2026-11-01)."""
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import crypto_caps as cc

NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=timezone.utc)


def ts(hours_ago, minutes=0):
    return (NOW - timedelta(hours=hours_ago, minutes=minutes)).strftime("%Y-%m-%d %H:%M:%SZ")


def monthly_head(t, net=0, rs=0, rl=0, bot="SOL-MIN", event="KXSOLMINMON-SOL-26NOV30"):
    return (f"{t} [{bot}] SOL=$93.25 dir=min sigma=0.0342 T=29.3d MTDlow=$93.23 "
            f"event={event} net={net:+.0f} risk short/long ${rs:.0f}/${rl:.0f} "
            f"sigma_raw=0.0271 sigma_med=0.0363")


def monthly_strike(t, pos, k="80", event="KXSOLMINMON-SOL-26NOV30"):
    return (f"{t}   {event}-{k}000 strike={k}: fair=44c ext 36/41 pos={pos:+.0f} "
            f"bids=[36, 34, 32] asks=[49, 51, 53]")


def ud_head(t, net, bot="SOL", tag="ud"):
    return f"{t} [{bot} {tag}] {bot}=$93.18 sigma_d=0.0305 (raw 0.0253, median 0.0350) net={net:+.0f} events=1"


def ud_event(t, net, rs, rl, cap, event="KXSOLD-26NOV0617", cad="weekly"):
    return (f"{t}   {event} [{cad}] 119.58h left, 50 strikes -> 8 quotable, net={net:+.0f}, "
            f"skew=+0.0c, risk short/long ${rs:.0f}/${rl:.0f} cap ${cap:g}")


def ud_strike(t, pos, event="KXSOLD-26NOV0617", k="92.9999"):
    return (f"{t}     {event}-T{k} K={k} fair=50c ext 47/58 pos={pos:+.0f} "
            f"bids=[45, 43, 41] asks=[58, 60, 62]")


LIMITS = {
    "monthly": {"position": 300, "event": 1500, "risk": 450, "reduce_only": {"*": 900}},
    "weekly": {"position": 80, "event": 400, "asset": 800, "risk": {"weekly": 120},
               "reduce_only": {"weekly": 240}},
}


class _Fleets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dirs = {f: os.path.join(self.tmp.name, f) for f in ("monthly", "weekly")}
        for d in self.dirs.values():
            os.makedirs(d)

    def specs(self):
        return [("monthly", self.dirs["monthly"], lambda k: f"{k.lower()}-2*.log",
                 LIMITS["monthly"]),
                ("weekly", self.dirs["weekly"], lambda k: f"{k.lower()}-*.log",
                 LIMITS["weekly"])]

    def status(self, fleet, key, fresh=True):
        upd = NOW if fresh else NOW - timedelta(hours=5)
        with open(os.path.join(self.dirs[fleet], f"status_{key}.json"), "w") as f:
            json.dump({"market": key, "updated_at": upd.strftime("%Y-%m-%dT%H:%M:%SZ")}, f)

    def log(self, fleet, name, lines, mtime_hours_ago=0):
        p = os.path.join(self.dirs[fleet], name)
        with open(p, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        t = (NOW - timedelta(hours=mtime_hours_ago)).timestamp()
        os.utime(p, (t, t))
        return p

    def report(self):
        return cc.cap_report(NOW, specs=self.specs())


class TestCapReport(_Fleets):
    def test_risk_cap_hit_then_cleared(self):
        self.status("monthly", "SOL-MIN")
        lines = [monthly_head(ts(10, 30 * i), rl=300 + 40 * i) for i in range(6)]   # 300..500
        lines += [monthly_head(ts(6, 30 * i), rl=300) for i in range(6)]            # back under
        self.log("monthly", "sol-min-2026-11-01.log", lines)
        rep = self.report()
        hits = [h for h in rep["hits"] if h["kind"] == "risk_long"]
        self.assertEqual(len(hits), 1)
        h = hits[0]
        self.assertEqual((h["peak"], h["limit"]), (500, 450))
        self.assertFalse(h["now"])                  # it went back under the cap
        self.assertEqual(h["count"], 2)             # 460 and 500 reached the cap
        self.assertIn("cleared", cc.hit_text(h))
        self.assertIn("$500 of $450", cc.hit_text(h))

    def test_position_cap_at_the_cap_now(self):
        self.status("monthly", "SOL-MIN")
        lines = []
        for i in range(4):
            t = ts(1, 30 - 10 * i)
            lines += [monthly_head(t, net=-300), monthly_strike(t, -300)]
        self.log("monthly", "sol-min-2026-11-01.log", lines)
        rep = self.report()
        pos = [h for h in rep["hits"] if h["kind"] == "position"]
        self.assertEqual(len(pos), 1)
        self.assertTrue(pos[0]["now"])
        text = cc.hit_text(pos[0])
        self.assertIn("AT THE CAP NOW", text)
        self.assertIn("position cap on the 80 strike: 300 short of 300", text)
        self.assertIn("1 HIT", cc.summary_text(rep))

    def test_weekly_event_risk_cap_comes_from_the_log_line(self):
        self.status("weekly", "SOL")
        t = ts(2)
        self.log("weekly", "sol-daily+weekly-2026-11-01.log",
                 [ud_head(t, -10), ud_event(t, -10, 125, 0, 120), ud_strike(t, -10)])
        rep = self.report()
        kinds = {(h["kind"], h["where"]) for h in rep["hits"]}
        self.assertEqual(kinds, {("risk_short", "KXSOLD-26NOV0617")})
        self.assertIn("on KXSOLD-26NOV0617", cc.hit_text(rep["hits"][0]))

    def test_weekly_reduce_only_event_and_asset_caps(self):
        self.status("weekly", "SOL")
        t = ts(3)
        self.log("weekly", "sol-daily+weekly-2026-11-01.log",
                 [ud_head(t, 820), ud_event(t, 410, 0, 50, 120)])
        kinds = {h["kind"] for h in self.report()["hits"]}
        self.assertEqual(kinds, {"reduce_only", "event", "asset"})

    def test_lines_older_than_the_window_do_not_count(self):
        self.status("monthly", "SOL-MIN")
        self.log("monthly", "sol-min-2026-11-01.log",
                 [monthly_head(ts(30), rl=999), monthly_head(ts(1), rl=10)])
        rep = self.report()
        self.assertEqual(rep["hits"], [])
        self.assertIn("none hit", cc.summary_text(rep))

    def test_stale_files_are_skipped_and_files_read_oldest_first(self):
        self.status("monthly", "SOL-MIN")
        self.log("monthly", "sol-min-2026-10-01.log", [monthly_head(ts(30), rl=999)],
                 mtime_hours_ago=29)                       # not touched in 24h: skipped
        self.log("monthly", "sol-min-2026-10-20.log", [monthly_head(ts(20), rl=460)],
                 mtime_hours_ago=19)
        self.log("monthly", "sol-min-2026-11-01.log", [monthly_head(ts(1), rl=10)])
        h = [h for h in self.report()["hits"] if h["kind"] == "risk_long"]
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["peak"], 460)
        self.assertFalse(h[0]["now"])                       # the newer file is under

    def test_fresh_bot_with_no_cycle_lines_is_blind(self):
        self.status("monthly", "SOL-MIN")
        self.status("monthly", "BTC-MAX", fresh=False)      # stale: health line's job
        self.log("monthly", "sol-min-2026-11-01.log", [f"{ts(1)} launcher: starting SOL-MIN live"])
        rep = self.report()
        self.assertEqual(rep["blind"], ["SOL-MIN monthly"])

    def test_backstop_line_is_a_hit(self):
        self.status("monthly", "SOL-MIN")
        t = ts(1)
        self.log("monthly", "sol-min-2026-11-01.log",
                 [monthly_head(t), f"{t} [SOL-MIN] ! side cap: NOT placing bid 8x @ 40c on KX"])
        h = [h for h in self.report()["hits"] if h["kind"] == "backstop"]
        self.assertEqual(len(h), 1)
        self.assertIn("should never fire", cc.hit_text(h[0]))

    def test_near_list_and_top(self):
        self.status("monthly", "SOL-MIN")
        self.log("monthly", "sol-min-2026-11-01.log", [monthly_head(ts(1), rl=400)])
        rep = self.report()
        self.assertEqual(rep["hits"], [])
        self.assertEqual([(i["kind"], round(i["ratio"], 2)) for i in rep["near"]],
                         [("risk_long", 0.89)])
        self.assertIn("highest use: SOL-MIN monthly $-at-risk cap, long side", cc.summary_text(rep))

    def test_failed_check_reads_not_checked(self):
        self.assertIn("NOT CHECKED", cc.summary_text(None))


class TestLinesSince(unittest.TestCase):
    def test_small_chunks_read_exactly_the_window(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.log")
            lines = [monthly_head(ts(48 - h), rl=h) for h in range(48)]
            with open(p, "w") as f:
                f.write("\n".join(lines) + "\n[create_order->V2] {no timestamp}\n")
            cutoff = ts(24)
            want = [ln for ln in lines if ln[:20] >= cutoff]
            for chunk in (64, 333, 4096, 1 << 20):
                self.assertEqual(list(cc.lines_since(p, cutoff, chunk=chunk)), want, chunk)


class TestLiveLimits(unittest.TestCase):
    def test_limits_follow_the_bot_classes(self):
        specs = {f: lim for f, _d, _g, lim in cc.fleet_specs()}
        self.assertEqual(specs["monthly"], {"position": 300, "event": 1500, "risk": 450,
                                            "reduce_only": {"*": 900}})
        self.assertEqual(specs["weekly"]["position"], 80)
        self.assertEqual(specs["weekly"]["event"], 400)
        self.assertEqual(specs["weekly"]["asset"], 800)
        self.assertEqual(specs["weekly"]["risk"], {"weekly": 120})
        self.assertEqual(specs["weekly"]["reduce_only"], {"weekly": 240})
        self.assertEqual(specs["annual"]["position"], 40)
        self.assertEqual(specs["annual"]["risk"], {"annual": 60})
        self.assertEqual(specs["annual"]["reduce_only"], {"annual": 120})


if __name__ == "__main__":
    unittest.main()
