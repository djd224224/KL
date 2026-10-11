"""Tests for imm_election_x2_rescore.py (hermetic: a synthetic dashboard model)."""
import unittest

import imm_election_x2_rescore as R

FIELDS = ["rew", "pnl", "real", "du", "xfer", "fills", "cts", "usd", "mk", "mk_n",
          "mk_cts", "rest", "pos0", "pos1", "mk0", "mk1", "settled", "cr", "cs", "fx"]


def row(rew, pnl, fills=0, cts=0, mk=0.0, cs=0.0):
    r = [0.0] * len(FIELDS)
    for k, v in (("rew", rew), ("pnl", pnl), ("fills", fills), ("cts", cts),
                 ("mk", mk), ("mk_cts", cts), ("cs", cs)):
        r[FIELDS.index(k)] = v
    return r


def model(days):
    """days: {iso: (election row, other row)}"""
    D = {"day_fields": FIELDS, "generated_et": "test", "days": {},
         "markets": {"KXSTATELEG-A": {"fam": "Elections", "pos": 1, "risk": 10.0, "u": -2.0,
                                      "cur": {"q": 2}},
                     "KXRAIN-X": {"fam": "Weather & quakes"}}}
    for i, (iso, (e, o)) in enumerate(sorted(days.items())):
        D["days"][iso] = {"s": i * 86400.0, "e": i * 86400.0 + 86400.0, "ok": True,
                          "m": {"KXSTATELEG-A": e, "KXRAIN-X": o}}
    return D


class RescoreTests(unittest.TestCase):
    def setUp(self):
        x1 = {f"2026-10-0{d}": (row(80, -45, 30, 1000, -23.0), row(500, -500)) for d in range(1, 10)}
        x1["2026-10-10"] = (row(80, -45, 30, 1000, -23.0), row(0, 0))
        x1["2026-10-11"] = (row(10, -40), row(0, 0))              # switch-over: neither side
        self.x1 = x1

    def test_only_the_family_counts_and_switch_day_is_excluded(self):
        days = dict(self.x1)
        days["2026-10-12"] = (row(160, -90, 60, 2000, -46.0), row(0, 0))
        D = model(days)
        fd = R.family_days(D, first="2026-10-01")
        before, after = R.split(fd, "2026-10-13")
        self.assertNotIn("2026-10-11", before + after)
        self.assertEqual(after, ["2026-10-12"])
        b = R.window(fd, before)
        self.assertAlmostEqual(b["net_day"], 35.0)
        self.assertAlmostEqual(b["mk_c"], -2.3)
        # today's partial day is not an x2 day
        self.assertEqual(R.split(fd, "2026-10-12")[1], [])

    def test_verdicts(self):
        b = {"net_day": 35.0}
        mk = lambda net, keep: {"n": 6, "net_day": net, "keep": keep}   # noqa: E731
        self.assertEqual(R.verdict(b, mk(70.0, 0.45))[0], "KEEP x2")
        self.assertEqual(R.verdict(b, mk(40.0, 0.45))[0], "WATCH")
        self.assertEqual(R.verdict(b, mk(70.0, 0.20))[0], "WATCH")
        self.assertEqual(R.verdict(b, mk(20.0, 0.30))[0], "BACK TO x1")
        self.assertEqual(R.verdict(b, {"n": 0})[0], "NO DATA")

    def test_render_and_book(self):
        days = dict(self.x1)
        days["2026-10-12"] = (row(160, -90, 60, 2000, -46.0), row(0, 0))
        D = model(days)
        fd = R.family_days(D, first="2026-10-01")
        before, after = R.split(fd, "2026-10-13")
        text, tag, b, a = R.render(D, fd, before, after, R.book_now(D))
        self.assertEqual(tag, "KEEP x2")
        self.assertIn("NET / day", text)
        self.assertEqual(R.book_now(D), {"quoted": 1, "positions": 1, "cost": 10.0, "unreal": -2.0})
        text.encode("ascii")                      # the task console is cp1252


if __name__ == "__main__":
    unittest.main()
