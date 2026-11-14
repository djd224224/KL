"""Tests for imm_cash, the IMM dashboard's cash chart (Jack 2026-11-14: "add a
time series chart view of cash balance and how it's changing throughout the
day e.g. what families are drawing cash or adding cash, rewards, deposits,
etc"). Pure functions only: no Kalshi reads, no live files."""
import json
import os
import shutil
import tempfile
import unittest

import imm_cash as ic

T0 = 1_794_571_200.0            # a bucket-aligned UTC instant


def read(ts, cash, R=None, dep=None, wd=0.0, src="live"):
    return {"ts": ts, "cash": cash, "R": R, "dep": dep, "wd": wd if dep is not None else None, "src": src}


def src_of(tk, kind, fid):
    return ("Fam " + tk.split("-")[0], "imm") if fid != "x" else ("Other trades", "bot")


def kinds(out):
    tot = {}
    for _b, _s, k, v in out["cells"]:
        tot[out["k"][k]] = round(tot.get(out["k"][k], 0.0) + v, 2)
    return tot


class GapItemsTests(unittest.TestCase):
    """What no record explains, reading to reading: transfers, collateral,
    then the rest (reward credits)."""

    def test_split_in_order_and_closes_the_interval(self):
        a = read(T0, 1000.0, R=500.0, dep=10000.0)
        b = read(T0 + 3600, 1900.0, R=650.0, dep=10400.0)
        items = ic.gap_items(a, b, -300.0, [(T0 + 60, 400.0)], [(T0 + 120, -50.0)])
        self.assertEqual([(t, k) for t, k, _ in items],
                         [(T0 + 60, "transfer"), (T0 + 120, "transfer"),
                          (T0 + 3600, "collateral"), (T0 + 3600, "reward")])
        v = {k: round(sum(x for _t, kk, x in items if kk == k), 2) for _t, k, _ in items}
        self.assertEqual(v, {"transfer": 350.0, "collateral": 150.0, "reward": 700.0})
        # a's cash + flows + the items = b's cash
        self.assertAlmostEqual(1000.0 - 300.0 + sum(x for _t, _k, x in items), 1900.0)

    def test_a_late_clearing_deposit_lands_on_the_reading(self):
        # the ACH dated days earlier (outside the interval) is in the totals'
        # move only: it lands on the later reading, not in the rewards
        a = read(T0, 1000.0, R=0.0, dep=28000.0)
        b = read(T0 + 600, 1500.0, R=0.0, dep=28500.0)
        items = ic.gap_items(a, b, 0.0, [(T0 - 5 * 86400, 500.0)], [])
        self.assertEqual(items, [(T0 + 600, "transfer", 500.0)])

    def test_unknown_totals_fall_back_to_the_dated_transfers(self):
        items = ic.gap_items(read(T0, 0.0), read(T0 + 600, 300.0), 0.0, [(T0 + 10, 300.0)], [])
        self.assertEqual(items, [(T0 + 10, "transfer", 300.0)])


class BuildTests(unittest.TestCase):

    def seg(self, d0, t0, t1, c0, c1, flows, R0=100.0, R1=100.0):
        return {"d0": d0, "a0": read(t0, c0, R=R0, dep=0.0, src="7am"),
                "a1": read(t1, c1, R=R1, dep=0.0, src="7am"),
                "flows": flows, "fills": len(flows), "setts": 0, "mism": 0}

    def test_path_ties_to_every_reading_and_cells_add_up(self):
        t1 = T0 + 86400
        flows = [[T0 + 1000, "KXA-1", "buy", -200.0, "f1"],
                 [T0 + 5000, "KXB-1", "sell", 50.0, "f2"],
                 [T0 + 9000, "KXC-1", "settle", 30.0, None],
                 [T0 + 40000, "KXD-1", "buy", -10.0, "x"]]
        mid = read(T0 + 20000, 900.0, R=140.0, dep=0.0)       # a live reading inside the day
        out = ic.build([self.seg("d", T0, t1, 1000.0, 1300.0, flows, R1=160.0)], None, [mid],
                       [], [], src_of, T0, t1)
        P = {p[0]: p[1] for p in out["path"]}
        self.assertEqual(P[round(T0)], 1000.0)
        self.assertEqual(P[round(T0 + 20000)], 900.0)
        self.assertEqual(P[round(t1)], 1300.0)
        self.assertEqual([r[0] for r in out["reads"]], [round(T0), round(T0 + 20000), round(t1)])
        tot = kinds(out)
        self.assertEqual(tot["buy"], -210.0)
        self.assertEqual(tot["sell"], 50.0)
        self.assertEqual(tot["settle"], 30.0)
        # 1000 -> 900: flows -120, collateral +40, the rest (rewards) -20;
        # 900 -> 1300: flows -10, collateral +20, rewards +390
        self.assertEqual(tot["collateral"], 60.0)
        self.assertEqual(tot["reward"], 370.0)
        self.assertAlmostEqual(sum(c[3] for c in out["cells"]), 300.0)
        names = {out["src"][c[1]] for c in out["cells"]}
        self.assertIn("Other trades", names)
        self.assertIn("Fam KXA", names)
        self.assertIn("Reward credits", names)
        # bucket cells: the 1000 s buy sits in bucket T0/600 + 1
        b = [c for c in out["cells"] if out["src"][c[1]] == "Fam KXA"]
        self.assertEqual(b[0][0], int((T0 + 1000) // 600))

    def test_the_live_span_runs_on_from_the_newest_snapshot(self):
        t1 = T0 + 86400
        seg = self.seg("d", T0, t1, 1000.0, 1000.0, [])
        live = {"a0": read(t1, 1000.0, R=100.0, dep=0.0, src="7am"),
                "flows": [[t1 + 700, "KXA-1", "buy", -40.0, "f9"]], "until": t1 + 1300}
        sample = read(t1 + 1300, 975.0, R=100.0, dep=0.0)
        out = ic.build([seg], live, [sample], [], [], src_of, T0, t1 + 1300)
        P = {p[0]: p[1] for p in out["path"]}
        self.assertEqual(P[round(t1 + 1300)], 975.0)
        self.assertEqual(kinds(out)["reward"], 15.0)
        self.assertEqual(out["until"], round(t1 + 1300))
        self.assertNotIn(None, [p[1] for p in out["path"]])

    def test_a_day_not_replayed_breaks_the_line(self):
        s1 = self.seg("a", T0, T0 + 86400, 1000.0, 1000.0, [])
        s3 = self.seg("c", T0 + 2 * 86400, T0 + 3 * 86400, 500.0, 500.0, [])
        out = ic.build([s1, s3], None, [], [], [], src_of, T0, T0 + 3 * 86400)
        self.assertIn(None, [p[1] for p in out["path"]])

    def test_nothing_before_the_start(self):
        t1 = T0 + 86400
        flows = [[T0 + 100, "KXA-1", "buy", -5.0, "f1"], [t1 - 100, "KXA-1", "buy", -5.0, "f2"]]
        out = ic.build([self.seg("d", T0, t1, 100.0, 90.0, flows)], None, [], [], [], src_of,
                       T0 + 43200, t1)
        self.assertEqual(kinds(out), {"buy": -5.0})
        self.assertTrue(all(p[0] >= T0 + 43200 for p in out["path"]))


class TiedTests(unittest.TestCase):
    """Jack 2026-11-14: "include the top events/markets that ties up cash (in
    case i need to emergency unwind it)"."""

    def test_exit_at_the_touch(self):
        self.assertAlmostEqual(ic.exit_value(10, 0.40, 0.45), 4.0)        # YES at the YES bid
        self.assertAlmostEqual(ic.exit_value(-10, 0.40, 0.45), 5.5)       # NO at 1 - the YES ask
        self.assertEqual(ic.exit_value(10, 0.0, 0.45), 0.0)               # no bid
        self.assertEqual(ic.exit_value(-10, 0.40, 0.0), 0.0)              # no YES ask = no NO bid

    def test_exclusive_event_nets_the_collateral_and_takes_it_back_on_exit(self):
        # three NO at 90c on mutually exclusive names: $2.70 paid, at most one
        # NO loses, so Kalshi ties up $0.70 and has handed back $2.00; selling
        # the three at a 92c NO bid brings in $2.76 less that $2.00
        pos = {f"KXWIN-26-{n}": {"ev": "KXWIN-26", "pos": -1.0, "cost": 0.9, "bid": 0.05,
                                 "ask": 0.08, "mark": 0.065, "shard": 0} for n in "ABC"}
        pos["KXBIG-1"] = {"ev": "KXBIG", "pos": 100.0, "cost": 50.0, "bid": 0.48, "ask": 0.52,
                          "mark": 0.5, "shard": 0}
        out = ic.tied(pos, {"KXWIN-26": 0.7, "KXBIG": 50.0},
                      lambda tk: ("Fam", "imm") if tk.startswith("KXBIG") else ("Other trades", "bot"),
                      {"KXBIG": "Big one"}, T0)
        ev = {r[0]: r for r in out["ev"]}
        self.assertEqual([r[0] for r in out["ev"]], ["KXBIG", "KXWIN-26"])     # biggest first
        self.assertEqual(ev["KXBIG"][1:4], ["Big one", "Fam", "imm"])
        self.assertEqual(ev["KXBIG"][7:9], [50.0, 48.0])
        self.assertEqual(ev["KXWIN-26"][7], 0.7)
        self.assertEqual(ev["KXWIN-26"][10], 2.7)                          # paid, before the return
        self.assertAlmostEqual(ev["KXWIN-26"][8], 0.76)
        self.assertEqual((out["tied"], out["cost"], out["n_ev"], out["n_mk"]), (50.7, 52.7, 2, 4))
        self.assertEqual(out["mk"][0][:5], ["KXBIG-1", "KXBIG", "Fam", "imm", 100.0])


class SampleTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_dedupes_and_trims(self):
        now = T0 + 100 * 86400
        self.assertTrue(ic.record_sample(self.dir, read(T0, 1.0), now))
        self.assertFalse(ic.record_sample(self.dir, read(T0, 1.0), now))   # cached read
        self.assertTrue(ic.record_sample(self.dir, read(now, 2.0), now))   # trims the old one
        rows = ic.load_samples(self.dir)
        self.assertEqual([r["cash"] for r in rows], [2.0])
        with open(os.path.join(self.dir, ic.SAMPLES_FILE), "a", encoding="utf-8") as f:
            f.write('{"ts": 1')                                              # a torn line
        self.assertEqual(len(ic.load_samples(self.dir)), 1)

    def test_live_read_and_snapshot_read(self):
        pf = {"cash": 11274.21, "collateral_returned": 14149.68, "deposited": 46900.0,
              "withdrawn": 0.0, "shards": {"rows": [{"shard": 0, "cash": 7162.77},
                                                     {"shard": 2, "cash": 3718.45}]}}
        r = ic.live_read(pf, T0)
        self.assertEqual((r["cash"], r["R"], r["sh"]), (11274.21, 14149.68, {"0": 7162.77, "2": 3718.45}))
        s = ic.snapshot_read({"created_utc": "2026-11-14T12:00:05Z", "cash": 5.0, "date": "2026-11-14"})
        self.assertEqual((s["src"], s["d"], s["R"]), ("7am", "2026-11-14", None))
        json.dumps(r)


if __name__ == "__main__":
    unittest.main()
