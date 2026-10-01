"""Tests for imm_gas_trial_report (the 1pm-midnight gas trial's 1-week and
2-week reports). Pure functions only: no bot import, no network, no email."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import imm_gas_trial_report as g

START = datetime(2026, 10, 1, 17, 30, tzinfo=timezone.utc)      # 13:30 ET


def _write(path, lines):
    with open(path, "w", encoding="utf-8") as f:
        for l in lines:
            f.write(l if isinstance(l, str) else json.dumps(l))
            f.write("\n")


class TestPieces(unittest.TestCase):
    def test_family_of(self):
        self.assertEqual(g.family_of("KXAAAGASD-26OCT02-4.4000"), "national")
        self.assertEqual(g.family_of("KXDIESELD-26OCT02-T6.470"), "diesel")
        self.assertEqual(g.family_of("KXAAAGASDCA-26OCT02-6.3950"), "state")
        for t in ("KXAAAGASW-26OCT05-4.40", "KXDIESELMONAK-26OCT02-T6.6", "KXAAAGASDNYC-26OCT02-4"):
            self.assertIsNone(g.family_of(t), t)

    def test_trial_start_is_the_first_startup_line(self):
        d = tempfile.mkdtemp(prefix="gt_")
        _write(os.path.join(d, "incentive-mm-2026-09-29.log"),
               ["2026-09-29 12:00:00Z gas trial: an old line before the window"])
        _write(os.path.join(d, "incentive-mm-2026-10-01.log"), [
            "2026-10-01 17:29:00Z mortgage gate: ...",
            "2026-10-01 17:30:00Z gas trial: KXAAAGASD,KXDIESELD quoted plain outside ...",
            "2026-10-01 18:00:00Z gas trial: a restart"])
        self.assertEqual(g.trial_start(d), START)
        self.assertIsNone(g.trial_start(tempfile.mkdtemp(prefix="gt_")))

    def test_fill_pnl_signs(self):
        buy = {"side": "yes", "yes_price_cents": 40.0, "count": 10.0}
        sell = {"side": "no", "yes_price_cents": 40.0, "count": 10.0}   # a YES sale at 40
        self.assertAlmostEqual(g.fill_pnl(buy, 1.0), 6.0)
        self.assertAlmostEqual(g.fill_pnl(buy, 0.0), -4.0)
        self.assertAlmostEqual(g.fill_pnl(sell, 1.0), -6.0)
        self.assertAlmostEqual(g.fill_pnl(sell, 0.0), 4.0)
        self.assertAlmostEqual(g.fill_pnl(buy, 0.55), 1.5)               # open, at mid

    def test_reward_scan_integrates_family_rows_only(self):
        d = tempfile.mkdtemp(prefix="gt_")
        hdr = ("ts,ticker,ext_bid,ext_ask,yes_depth,no_depth,target,est_frac,qual_sides,"
               "acct_pos,own_pos,pool_per_day,quoted")

        def row(ts, t, frac, pool):
            return f"{ts},{t},40,42,2000,2000,1000,{frac},2,0,0,{pool},20"
        t0 = START + timedelta(minutes=10)
        ts = [(t0 + timedelta(seconds=s)).strftime("%Y-%m-%dT%H:%M:%SZ")
              for s in (0, 30, 60, 60 + 3600)]
        _write(os.path.join(d, "cycle_log_2026-10-01.csv"), [hdr] + [
            row(ts[0], "KXAAAGASD-26OCT02-4.40", 0.1, 100.0),
            row(ts[0], "KXGOOD-99DEC31-A", 0.5, 100.0),
            row(ts[1], "KXAAAGASD-26OCT02-4.40", 0.1, 100.0),
            row(ts[2], "KXGOOD-99DEC31-A", 0.5, 100.0),
            row(ts[2], "KXDIESELD-26OCT02-T6.47", 0.2, 50.0),
            row(ts[3], "KXAAAGASD-26OCT02-4.40", 0.1, 100.0)])     # a 1h gap: capped
        est = g.scan_rewards(d, START, START + timedelta(days=1))
        self.assertEqual(set(est), {"KXAAAGASD-26OCT02-4.40", "KXDIESELD-26OCT02-T6.47"})
        # first cycle of the file has no dt; then 30s, then the gap capped at 900s
        self.assertAlmostEqual(est["KXAAAGASD-26OCT02-4.40"], 0.1 * 100 * (30 + 900) / 86400.0)
        self.assertAlmostEqual(est["KXDIESELD-26OCT02-T6.47"], 0.2 * 50 * 30 / 86400.0)
        # nothing before the trial start counts
        self.assertEqual(g.scan_rewards(d, START + timedelta(days=2), START + timedelta(days=3)), {})

    def test_due_mark(self):
        self.assertIsNone(g.due_mark(3.0, set()))
        self.assertEqual(g.due_mark(7.2, set()), ("1w", "1-week", ["1w"]))
        self.assertIsNone(g.due_mark(9.0, {"1w"}))
        self.assertEqual(g.due_mark(14.1, {"1w"}), ("2w", "2-week", ["2w"]))
        # a missed 1-week folds into the 2-week; both get marked
        self.assertEqual(g.due_mark(15.0, set()), ("2w", "2-week", ["1w", "2w"]))
        self.assertIsNone(g.due_mark(20.0, {"1w", "2w"}))


class TestBuild(unittest.TestCase):
    def test_end_to_end_without_the_network(self):
        d = tempfile.mkdtemp(prefix="gt_")
        t = START.timestamp()
        iso = lambda s: datetime.fromtimestamp(t + s, timezone.utc).isoformat()   # noqa: E731
        _write(os.path.join(d, "orders_2026-10-01.jsonl"), [
            {"ts": iso(60), "kind": "place", "ticker": "KXAAAGASD-26OCT02-4.40"},
            {"ts": iso(7200), "kind": "amend", "ticker": "KXAAAGASD-26OCT02-4.40"},
            {"ts": iso(120), "kind": "place", "ticker": "KXDIESELD-26OCT02-T6.47"},
            {"ts": iso(180), "kind": "cancel", "ticker": "KXDIESELD-26OCT02-T6.47"},
            {"ts": iso(-600), "kind": "place", "ticker": "KXAAAGASD-26OCT02-4.40"}])
        _write(os.path.join(d, "fills_2026-10-01.jsonl"), [
            {"ts": t + 600, "ticker": "KXAAAGASD-26OCT02-4.40", "event_ticker": "KXAAAGASD-26OCT02",
             "side": "yes", "yes_price_cents": 40.0, "count": 20.0},
            {"ts": t + 4000, "ticker": "KXDIESELD-26OCT02-T6.47", "event_ticker": "KXDIESELD-26OCT02",
             "side": "no", "yes_price_cents": 70.0, "count": 10.0},
            {"ts": t - 60, "ticker": "KXAAAGASD-26OCT02-4.40", "event_ticker": "KXAAAGASD-26OCT02",
             "side": "yes", "yes_price_cents": 99.0, "count": 50.0}])           # before the start

        def fake(path, params, key):
            if params["event_ticker"] == "KXAAAGASD-26OCT02":
                return [{"ticker": "KXAAAGASD-26OCT02-4.40", "result": "yes"}]
            return [{"ticker": "KXDIESELD-26OCT02-T6.47", "result": "",
                     "yes_bid_dollars": "0.6000", "yes_ask_dollars": "0.6400"}]
        ctx = g.build(d, START, START + timedelta(days=7, hours=1), get_all=fake)
        nat, dsl = ctx["fams"]["national"], ctx["fams"]["diesel"]
        self.assertEqual((nat["fills"], nat["contracts"]), (1, 20.0))
        self.assertAlmostEqual(nat["settled"], 12.0)                 # 20 x (1 - 0.40)
        self.assertAlmostEqual(dsl["open"], 0.8)                     # 10 x (0.70 - 0.62)
        self.assertEqual(len(nat["hours"]), 2)
        self.assertEqual(nat["early"], 0)                            # the -600s place is pre-trial
        self.assertEqual(ctx["state_orders"], 0)
        self.assertEqual(nat["verdict"], "KEEP")
        text = g.render(ctx, "1-week")
        text.encode("cp1252")
        self.assertIn("IMM gas trial -- 1-week report", text)
        self.assertIn("NATIONAL (KXAAAGASD)", text)
        self.assertIn("September baseline", text)
        self.assertNotIn("nan", text)

    def test_stop_verdict_when_trading_loss_beats_the_reward(self):
        d = {"fills": 5, "hours": {("2026-10-01", 14)}, "trading": -3.0, "est_floored": 1.2}
        self.assertTrue(g.verdict(d).startswith("STOP"))
        self.assertEqual(g.verdict(dict(d, trading=-1.0)), "KEEP")
        self.assertTrue(g.verdict({"fills": 0, "hours": set(), "trading": 0.0,
                                   "est_floored": 0.0}).startswith("NOT QUOTING"))


if __name__ == "__main__":
    unittest.main()
