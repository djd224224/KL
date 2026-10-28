"""imm_astest_picks.py -- the AS test's daily picks (Jack 2026-10-27: "pick 5
distinct events each day, optimizing for likelihood of profitability, even
better if fair value can be built, quote it at 1x size"). The scoring, the
distinct choice and the tape arithmetic; run with IMM_STATUS_DIR at a scratch
dir like every IMM test."""
import unittest
from datetime import datetime, timedelta, timezone

import imm_astest_picks as P


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def row(event, p, net, n=5, fair="none"):
    return {"event": event, "series": event.split("-")[0], "fair": fair,
            "score": {"n": n, "p_profit": p, "net_mean": net, "net_p25": net,
                      "fills": 0.0, "cost_c": 1.0}}


class TestScore(unittest.TestCase):

    def test_fills_priced_at_three_times_the_makers_loss_never_under_a_cent(self):
        # 100 ct/h at a 10% touch share for 8h = 80 fills
        s = P.score_event(10.0, 0.10, 8.0, [(100.0, -2.0), (100.0, 1.5), (100.0, -10.0)])
        # costs 6c, 1c (makers made money: the floor), 30c
        nets = [10.0 - 80 * 0.06, 10.0 - 80 * 0.01, 10.0 - 80 * 0.30]
        self.assertEqual(s["n"], 3)
        self.assertAlmostEqual(s["net_mean"], sum(nets) / 3)
        self.assertAlmostEqual(s["p_profit"], 2 / 3)
        self.assertAlmostEqual(s["net_p25"], min(nets))
        self.assertAlmostEqual(s["fills"], 80.0)

    def test_the_fill_pause_caps_fills_an_hour(self):
        s = P.score_event(5.0, 1.0, 2.0, [(10_000.0, -1.0)])
        self.assertAlmostEqual(s["fills"], P.FILL_CAP_PER_H * 2.0)

    def test_no_history_scores_nothing(self):
        s = P.score_event(50.0, 0.5, 24.0, [])
        self.assertEqual(s["n"], 0)
        self.assertFalse(P.eligible({"score": s}))

    def test_touch_share_queues_behind_the_resting_size(self):
        self.assertAlmostEqual(P.touch_share(20.0, 180.0), 0.1)
        self.assertAlmostEqual(P.touch_share(20.0, 0.0), 1.0)
        self.assertEqual(P.touch_share(20.0, None), 0.5)


class TestChoose(unittest.TestCase):

    def test_likeliest_first_then_a_fair_then_the_net(self):
        rows = [row("KXA-26NOV01", 0.8, 9.0), row("KXB-26NOV01", 1.0, 1.0),
                row("KXC-26NOV01", 0.8, 2.0, fair="buildable"),
                row("KXD-26NOV01", 0.4, 50.0),                 # not likely enough
                row("KXE-26NOV01", 1.0, 0.2),                  # too small
                row("KXF-26NOV01", 1.0, 30.0, n=2)]            # too little history
        got = [r["event"] for r in P.choose(rows)]
        self.assertEqual(got, ["KXB-26NOV01", "KXC-26NOV01", "KXA-26NOV01"])

    def test_distinct_one_per_series_and_one_gas_state_a_region(self):
        rows = [row("KXA-26NOV01", 1.0, 9.0), row("KXA-26NOV08", 1.0, 8.0),
                row("KXAAAGASDCT-26OCT29", 1.0, 7.0), row("KXAAAGASDMA-26OCT29", 1.0, 6.0),
                row("KXAAAGASDTX-26OCT29", 1.0, 5.0), row("KXB-26NOV01", 1.0, 4.0),
                row("KXC-26NOV01", 1.0, 3.0), row("KXD-26NOV01", 1.0, 2.0)]
        got = [r["event"] for r in P.choose(rows)]
        # CT and MA share New England; TX is the Gulf Coast's
        self.assertEqual(got, ["KXA-26NOV01", "KXAAAGASDCT-26OCT29",
                               "KXAAAGASDTX-26OCT29", "KXB-26NOV01", "KXC-26NOV01"])
        self.assertEqual(P.driver_group("KXAAAGASDCT"), P.driver_group("KXAAAGASDMA"))
        self.assertNotEqual(P.driver_group("KXAAAGASDCT"), P.driver_group("KXAAAGASDTX"))
        self.assertEqual(P.driver_group("KXAAAGASD"), "KXAAAGASD")

    def test_yesterdays_eligible_picks_keep_their_seats(self):
        rows = [row(f"KX{c}-26NOV01", 1.0, 10.0 - i) for i, c in enumerate("ABCDEF")]
        got = [r["event"] for r in P.choose(rows, incumbents=frozenset({"KXF-26NOV01"}))]
        self.assertEqual(got[0], "KXF-26NOV01")
        self.assertEqual(len(got), P.PICK_COUNT)
        self.assertNotIn("KXE-26NOV01", got)
        # ...but not once they stop being eligible
        rows[-1]["score"]["p_profit"] = 0.2
        got = [r["event"] for r in P.choose(rows, incumbents=frozenset({"KXF-26NOV01"}))]
        self.assertNotIn("KXF-26NOV01", got)


class TestSourcesAndWindows(unittest.TestCase):

    def test_fair_buildable_on_a_public_statistic_and_kpis_flagged(self):
        self.assertEqual(P.fair_status(["AAA https://gasprices.aaa.com"]),
                         ("buildable", "aaa.com"))
        self.assertEqual(P.fair_status(["Some Blog https://example.com"]), ("none", ""))
        self.assertTrue(P.kpi_series(["Fiscal.ai https://fiscal.ai/company"]))
        self.assertFalse(P.kpi_series(["BLS https://www.bls.gov"]))

    def test_quote_hours_inside_a_daily_window(self):
        # EDT: 16:00-24:00 ET = 20:00-04:00Z
        start, end = utc(2026, 10, 27, 14, 0), utc(2026, 10, 28, 4, 0)
        self.assertAlmostEqual(P.quote_hours(start, end, ("16:00", "24:00")), 8.0)
        self.assertAlmostEqual(P.quote_hours(start, end, None), 14.0)
        # a window past midnight (20:00-05:00 ET)
        self.assertAlmostEqual(P.quote_hours(utc(2026, 10, 27, 23, 0), utc(2026, 10, 28, 11, 0),
                                             ("20:00", "05:00")), 9.0)

    def test_the_picks_hours_as_hours_before_the_cutoff(self):
        cutoff = utc(2026, 10, 28, 4, 0)                    # 00:00 ET
        hbs = P.quoted_hbs(utc(2026, 10, 27, 14, 30), cutoff, cutoff, ("16:00", "24:00"))
        self.assertEqual(sorted(hbs), list(range(8)))
        far = P.quoted_hbs(utc(2026, 10, 1, 4, 0), utc(2026, 10, 1, 6, 0), cutoff, None)
        self.assertEqual(far, [P.HIST_MAX_HB, P.HIST_MAX_HB])


class TestTape(unittest.TestCase):

    def test_makers_pnl_by_hour_before_the_cutoff(self):
        cutoff = utc(2026, 10, 28, 4, 0)
        trades = [
            # a taker bought YES 10 @ 60c on a market that settled NO: makers +$6
            {"created_time": "2026-10-28T03:30:00Z", "yes_price_dollars": "0.60",
             "count_fp": "10.00", "taker_side": "yes"},
            # a taker sold YES (bought NO) 5 @ 40c: makers bought YES at 40, -$2
            {"created_time": "2026-10-28T01:10:00Z", "yes_price_dollars": "0.40",
             "count_fp": "5.00", "taker_side": "no"},
            # after the cutoff: the bot is out, not counted
            {"created_time": "2026-10-28T04:30:00Z", "yes_price_dollars": "0.10",
             "count_fp": "99.00", "taker_side": "yes"}]
        agg = P.tape_aggregate(trades, cutoff, 0.0)
        self.assertEqual(sorted(agg), ["0", "2"])
        self.assertAlmostEqual(agg["0"][0], 10.0)
        self.assertAlmostEqual(agg["0"][1], 6.0)
        self.assertAlmostEqual(agg["2"][1], -2.0)
        v, maker_c = P.event_rate([{"hb": agg}], [0, 1, 2], life_h=20.0)
        self.assertAlmostEqual(v, 5.0)                       # 15 ct over 3 hours
        self.assertAlmostEqual(maker_c, 100 * 4.0 / 15.0)    # under 50 ct: the whole tape
        # an event that never lived through the pick's hours says nothing
        self.assertIsNone(P.event_rate([{"hb": agg}], [30, 31], life_h=10.0))

    def test_a_settled_events_cutoff_takes_the_live_events_offset(self):
        m = {"occurrence_datetime": "", "expected_expiration_time": "2026-10-28T14:00:00Z"}
        base = P.base_cutoff("KXFOO-26OCT28", m)
        self.assertEqual(base, utc(2026, 10, 28, 4, 0))      # 00:00 ET of the ticker date
        self.assertEqual(P.past_cutoff("KXFOO-26OCT28", [m], timedelta(hours=6)),
                         utc(2026, 10, 27, 22, 0))


if __name__ == "__main__":
    unittest.main()
