"""Tests for imm_allocation.py (evidence section 14: where the next dollar of
size goes). A synthetic moat (family_daily.csv) and change registry
(changes.jsonl) in a temp dir; nothing reads or writes the live tree."""

import json
import os
import random
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import imm_allocation as al
import imm_review as rv
import send_imm_review as sr

DAY = date(2026, 11, 25)


def et_noon(d):
    return datetime(d.year, d.month, d.day, 12, tzinfo=rv.ET).timestamp()


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.status = os.path.join(self.root, "run-logs", "incentive-mm")
        self.review = os.path.join(self.status, "daily_review")
        os.makedirs(os.path.join(self.review, "moat"))
        rv.configure(status_dir=self.status, review_dir=self.review, repo=self.root)
        # each readout's family from its series, by a fixed map (the dashboard's rules in production)
        fam = {**{f"KXADD{i}": "Waves" for i in range(4)}, "KXADD4": "Good", "KXADD5": "Good",
               **{f"KXCUT{i}": "Cutters" for i in range(5)}, "KXTINY": "Cutters"}
        p = mock.patch.object(rv, "family_of", side_effect=lambda s: (fam.get(s, s), fam.get(s, s)))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        rv.configure()
        shutil.rmtree(self.root, ignore_errors=True)

    def family_daily(self, fams, days=30, seed=7):
        """fams: {name: (rew, pnl, rest, noise, n_days, share)} -> 30 ET days to DAY."""
        rnd = random.Random(seed)
        rows = []
        for i in range(days):
            d = (DAY - timedelta(days=days - 1 - i)).isoformat()
            for f, (rew, pnl, rest, noise, n_days, share) in fams.items():
                if i < days - n_days:
                    continue
                rows.append({"day": d, "family": f, "rew": round(rew + rnd.uniform(-noise, noise), 4),
                             "pnl": round(pnl + rnd.uniform(-noise, noise), 4), "rest": rest, "cts": 100.0,
                             "fills": 10, "share": share if share is not None and i >= days - 8 else None})
        for r in rows:
            r["net"] = round(r["rew"] + r["pnl"], 4)
        rv._write_rows(os.path.join(rv.MOAT_DIR, "family_daily.csv"), rows, rv.FAMILY_DAILY_COLS)

    def readout(self, split, rest_b, rest_a, rew_b, rew_a, pnl_b, pnl_a, sup=1.0, key="k1", scope=None,
                complete=True, book=False):
        sk = scope or {"series": [f"KX{key.upper()}"]}
        b = {"rest": rest_b, "rew": rew_b, "pnl": pnl_b, "net": rew_b + pnl_b}
        a = {"rest": rest_a, "rew": rew_a, "pnl": pnl_a, "net": rew_a + pnl_a}
        return {"op": "readout", "day": DAY.isoformat(), "keys": [key], "horizon": 7,
                "result": {"split": split, "horizon": 7, "scope": key, "scope_key": sk, "book": book,
                           "complete": complete, "before": b, "after": a, "supply_ratio": sup,
                           "series_sample": sk.get("series", [])}}

    def changes(self, rows):
        with open(os.path.join(rv.REVIEW_DIR, "changes.jsonl"), "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    def history(self, extra=(), good_pays=True):
        """6 adds that doubled capital: 4 in "Waves" earned no more rewards, 2
        in "Good" earned in proportion (unless good_pays=False); 5 cuts
        ("Cutters") that behaved as the proportional model says; and the
        readouts that must not count: supply moved, a small move, book-wide,
        incomplete, a duplicate."""
        rows = []
        for i in range(6):
            k = f"add{i}"
            rows.append({"op": "register", "key": k, "subject": f"imm: {k} x2"})
            rows.append(self.readout(f"2026-11-0{i + 1}", 10000.0 * (i + 1), 20000.0 * (i + 1), 50.0,
                                     100.0 if i >= 4 and good_pays else 50.0, -10.0, -12.0, key=k))
        for i in range(5):
            k = f"cut{i}"
            rows.append({"op": "register", "key": k, "subject": f"imm: {k} halved"})
            rows.append(self.readout(f"2026-11-1{i}", 10000.0 * (i + 1), 5000.0 * (i + 1), 40.0, 20.0,
                                     -30.0, -15.0, key=k))
        rows += [self.readout("2026-11-12", 10000, 30000, 50, 500, 0, 0, sup=2.0, key="sup"),
                 self.readout("2026-11-12", 10000, 11000, 50, 55, 0, 0, key="small"),
                 self.readout("2026-11-12", 10000, 30000, 50, 50, 0, 0, key="book", book=True),
                 self.readout("2026-11-12", 10000, 30000, 50, 50, 0, 0, key="part", complete=False),
                 self.readout("2026-11-12", 10000, 900, 50, 4.5, 0, 0, key="tiny"),    # 0.09x: counts
                 self.readout("2026-11-01", 10050, 20100, 50, 50, -10, -12, key="dup")]  # = add0's money
        self.changes(rows + list(extra))


class TrackRecordTests(Base):
    def test_only_real_capital_moves_count_and_each_direction_is_calibrated(self):
        self.history()
        moves = al.capital_moves(DAY)
        keys = sorted(m["scope"] for m in moves)
        self.assertEqual(keys, sorted([f"add{i}" for i in range(6)] + [f"cut{i}" for i in range(5)] + ["tiny"]))
        adds_m = [m for m in moves if m["k"] > 1]
        cuts_m = [m for m in moves if m["k"] < 1]
        self.assertEqual({m["family"] for m in adds_m}, {"Waves", "Good"})
        add_cal = al.family_calibration(adds_m, ["Stars"])
        cut_cal = al.family_calibration(cuts_m, ["Stars"])
        adds, cuts = al.direction(adds_m, add_cal), al.direction(cuts_m, cut_cal)
        self.assertEqual((adds["n"], adds["calibrated"]), (6, True))
        # in dollars: 300 predicted, 100 measured; each readout one vote: 2 of 6 paid in full
        self.assertEqual((adds["pred_rew"], adds["meas_rew"], adds["pool_rew"], adds["pool_pnl"]),
                         (300.0, 100.0, 0.333, 0.2))
        self.assertEqual(adds["marginal_per_1k"], round(1000 * (100 - 12.0) / 210000.0, 2))
        by = {b["family"]: b for b in adds["by_family"]}
        self.assertEqual((by["Waves"]["own_rew"], by["Good"]["own_rew"]), (0.0, 1.0))
        # each family's own ratio, shrunk toward the OTHER families' by 5 readouts
        self.assertEqual(add_cal["Waves"], (round(5 / 9, 4), 0.2))
        self.assertEqual(add_cal["Good"], (round(2 / 7, 4), 0.2))
        self.assertEqual(add_cal["Stars"], (round(2 / 6, 4), 0.2))   # no readouts: the book's mean
        self.assertEqual((cuts["n"], cut_cal["Cutters"], cut_cal["Stars"]), (6, (1.0, 1.0), (1.0, 1.0)))
        self.assertEqual(al.direction([], {})["calibrated"], False)
        # a readout read again (extended) replaces the earlier one
        self.history([self.readout("2026-11-01", 10000.0, 20000.0, 50.0, 80.0, -10.0, -12.0, key="add0")])
        add0 = [m for m in al.capital_moves(DAY) if m["scope"] == "add0"]
        self.assertEqual([m["rew_a"] for m in add0], [80.0])

    def test_the_model_and_the_calibration_rules(self):
        self.assertAlmostEqual(al.gain(100, -40, 0.0, 1.5, 1.0, 1.0), 50 - 20)
        self.assertAlmostEqual(al.gain(90, 0, 0.5, 2.0, 1.0, 1.0), 90 * (2 / 1.5 - 1))   # a full share saturates
        self.assertAlmostEqual(al.gain(100, -40, None, 2 / 3, 1.0, 0.5), 100 * (2 / 3 - 1) + 0.5 * -40 * (2 / 3 - 1))
        self.assertEqual(al.gain(100, -40, 0.0, 1.5, 0.0, 0.0), 0.0)
        self.assertEqual((al._ratio(-5, 10), al._ratio(50, 10), al._ratio(1, 0)), (0.0, 2.0, None))
        # a readout that predicted under $1/day has no ratio of its own
        m = {"k": 2.0, "rew_b": 0.5, "rew_a": 9.0, "pnl_b": -10.0, "pnl_a": -15.0}
        self.assertEqual(al.readout_ratios(m), (None, 0.5))
        # fewer than CAL_MIN_N readouts: the proportional model for everyone
        few = [{"family": "A", "r_rew": 0.0, "r_pnl": 0.0}] * (al.CAL_MIN_N - 1)
        self.assertEqual(al.family_calibration(few, ["B"]), {"A": (1.0, 1.0), "B": (1.0, 1.0)})

    def test_one_big_wave_cannot_set_every_familys_calibration(self):
        # the 186f8da flaw: dollar-weighted, 8 election readouts with 50x the money
        # zeroed every family's adds. Each readout one vote, shrunk per family:
        wave = [{"family": "Elections", "r_rew": 0.0, "r_pnl": 0.5, "rew_b": 5000.0, "k": 2.0}] * 8
        rt = [{"family": "RT", "r_rew": 1.0, "r_pnl": 0.5, "rew_b": 100.0, "k": 2.0}] * 4
        cal = al.family_calibration(wave + rt, ["AI"])
        self.assertEqual(cal["Elections"][0], round((0 + 5 * 1.0) / 13, 4))   # its own 0, pulled up
        self.assertEqual(cal["RT"][0], round((4 + 5 * 0.0) / 9, 4))           # its own 1, pulled down
        self.assertEqual(cal["AI"][0], round(4 / 12, 4))                     # the book: 4 of 12 votes paid
        self.assertEqual({c[1] for c in cal.values()}, {0.5})


class SectionTests(Base):
    FAMS = {
        "Losers": (10.0, -40.0, 5000.0, 4.0, 30, 0.04),     # trading eats the rewards: a cut gains
        "Recent": (10.0, -40.0, 5000.0, 4.0, 30, 0.04),     # the same, but changed 2 days ago: waits
        "Stars": (100.0, 5.0, 4000.0, 6.0, 30, 0.05),       # the best-measured: the add experiment
        "Thin": (10.0, -60.0, 5000.0, 2.0, 5, None),        # 5 days: never proposed
        "Steady": (30.0, -5.0, 20000.0, 3.0, 30, None),     # a cut loses money: nothing to do
    }

    def setup_book(self):
        self.family_daily(self.FAMS)
        self.history([{"op": "register", "key": "rec1", "subject": "imm: Recent family re-tuned",
                       "scope": {"basis": "family in the subject", "families": ["Recent"]},
                       "live_at": et_noon(DAY - timedelta(days=2))},
                      {"op": "register", "key": "old1", "subject": "imm: Losers tuned a month ago",
                       "scope": {"families": ["Losers"]}, "live_at": et_noon(DAY - timedelta(days=20))},
                      {"op": "register", "key": "book1", "subject": "imm: a book-wide change",
                       "scope": {"basis": "names no series, wave or family"},
                       "live_at": et_noon(DAY - timedelta(days=1))}])

    def test_moves_use_each_familys_calibration_and_a_changed_family_waits(self):
        self.setup_book()
        sec = al.sec_allocation(DAY)
        self.assertEqual(sec["window"]["days"], 30)
        props = {p["kind"]: p for p in sec["proposals"]}
        self.assertEqual({(k, p["family"]) for k, p in props.items()}, {("cut", "Losers"), ("add", "Stars")})
        cut, add = props["cut"], props["add"]
        self.assertGreater(cut["p10"], 0)
        self.assertAlmostEqual(cut["p50"], 10.0, delta=1.5)    # cut 1/3: +13.3 trading, -3.3 rewards
        # Stars has no readouts of its own: the book's 1-in-3 for rewards, 0.2 for trading
        self.assertEqual(add["cal"], {"rew": round(2 / 6, 4), "pnl": 0.2})
        self.assertGreater(add["p10"], 0)
        self.assertAlmostEqual(add["p50"], 100 * (1.5 / 1.025 - 1) / 3 + 0.2 * 5 * 0.5, delta=2.0)
        self.assertEqual([p["family"] for p in sec["proposals"]], ["Stars", "Losers"])   # by p50
        self.assertEqual([(w["kind"], w["family"]) for w in sec["waits"]], [("cut", "Recent")])
        self.assertEqual(sec["waits"][0]["recent"][0]["key"], "rec1")
        self.assertIsNone(sec["experiment"])                       # an add cleared the bar itself
        fam = {r["family"]: r for r in sec["families"]}
        self.assertEqual(fam["Stars"]["cal"]["add"], (round(2 / 6, 4), 0.2))
        self.assertEqual(fam["Thin"]["days"], 5)
        self.assertLess(fam["Steady"]["cut"]["p50"], 0)
        self.assertEqual(fam["Losers"]["recent"], [])             # its change was 20 days ago
        self.assertAlmostEqual(fam["Losers"]["share"], 0.04)
        # the same day gives the same intervals
        self.assertEqual(al.sec_allocation(DAY, log=False)["proposals"], sec["proposals"])
        # logged once a day, with the book's calibration beside each move's own
        rows = list(rv._iter_jsonl(os.path.join(rv.MOAT_DIR, al.ALLOC_LOG)))
        self.assertEqual([(r["kind"], r["family"]) for r in rows], [("add", "Stars"), ("cut", "Losers")])
        self.assertEqual((rows[0]["book_cal"]["adds"]["pool_rew"], rows[0]["cal"]["rew"]), (0.333, round(2 / 6, 4)))
        self.assertEqual(al.sec_allocation(DAY)["logged"], 0)

    def test_when_adds_have_paid_nothing_the_best_family_is_an_add_experiment(self):
        self.family_daily(self.FAMS)
        self.history(good_pays=False)
        sec = al.sec_allocation(DAY, log=False)
        self.assertTrue(all(p["kind"] == "cut" for p in sec["proposals"]))   # no add clears the bar
        ex = sec["experiment"]
        self.assertEqual((ex["family"], ex["k"], ex["cal"]), ("Stars", al.EXP_STEP, {"rew": 0.0, "pnl": 0.2}))
        self.assertGreater(ex["proportional"], ex["calibrated"])
        self.assertAlmostEqual(ex["calibrated"], 0.2 * 5 * 0.25, places=1)

    def test_a_day_with_nothing_to_do_logs_a_none_row(self):
        # a noisy near-zero family: no cut or add gains, and t is far under 2
        self.family_daily({"Noisy": (30.0, -28.0, 20000.0, 40.0, 30, None)})
        self.history()
        sec = al.sec_allocation(DAY)
        self.assertEqual((sec["proposals"], sec["experiment"]), ([], None))
        rows = list(rv._iter_jsonl(os.path.join(rv.MOAT_DIR, al.ALLOC_LOG)))
        self.assertEqual([r["kind"] for r in rows], ["none"])

    def test_without_a_track_record_the_proportional_model_stands_in(self):
        self.family_daily({"Stars": self.FAMS["Stars"]})
        self.changes([])
        sec = al.sec_allocation(DAY, log=False)
        self.assertEqual((sec["track"]["adds"]["n"], sec["track"]["adds"]["calibrated"]), (0, False))
        # uncalibrated, a profitable family's add clears the bar on the proportional model
        self.assertEqual([(p["kind"], p["family"]) for p in sec["proposals"]], [("add", "Stars")])
        self.assertIsNone(sec["experiment"])

    def test_render_chip_and_the_switch(self):
        self.setup_book()
        sec = al.sec_allocation(DAY, log=False)
        L = []
        rv.render_allocation(sec, L.append)
        text = "\n".join(L)
        for s in ("## 14. Capital allocation", "Adds: 6 readouts",
                  "Each readout one vote, the book's ratio: rewards x0.33, trading x0.20",
                  "  - Good: 2 readouts", "used x0.29 / x0.20", "  - Waves: 4 readouts", "used x0.56 / x0.20",
                  "- CUT Losers x0.67", "- ADD Stars x1.50", "WAITS: cut Recent", "rec1", "| Losers |"):
            self.assertIn(s, text)
        self.assertIn("x0.33 / x0.20 |", [l for l in L if l.startswith("| Stars |")][0])
        self.assertNotIn("ADD EXPERIMENT", text)
        chips = dict(sr.health_checks({"allocation": sec}))
        self.assertIn("Allocation: 2 moves, +$", " ".join(chips))
        none = dict(sr.health_checks({"allocation": dict(sec, proposals=[])}))
        self.assertEqual(none.get("Allocation: no move clears the bar"), "gray")
        ex = dict(sr.health_checks({"allocation": dict(sec, proposals=[], experiment={"family": "Stars"})}))
        self.assertEqual(ex.get("Allocation: no move clears the bar; Stars add experiment"), "gray")
        self.assertFalse([c for c, _ in sr.health_checks({"allocation": {"error": "x"}}) if c.startswith("Allocation")])
        L = []
        rv.render_allocation({}, L.append)
        self.assertIn("IMM_REVIEW_ALLOCATION=0", "\n".join(L))
        with mock.patch.dict(os.environ, {"IMM_REVIEW_ALLOCATION": "0"}):
            ev = rv.collect(DAY, only=["allocation"])
        self.assertNotIn("allocation", ev)
        ev = rv.collect(DAY, only=["allocation"])
        self.assertEqual([p["family"] for p in ev["allocation"]["proposals"]], ["Stars", "Losers"])


if __name__ == "__main__":
    unittest.main()
