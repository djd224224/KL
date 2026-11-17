#!/usr/bin/env python3
"""Unit tests for listed_combo_fair.py (no network) -- run: python -m unittest test_listed_combo_fair

Fixtures: the 11/17 13:30Z reads -- KXUSAIRANAGREEMENT-27-28 26/27, KXPRESPARTY-2028 D 67/68 and
R 32/33, Feb-2028 WTI (CLG28) $71.54 against an $86.86 front month -- and the grids' own cell
titles as Kalshi lists them."""

import math
import unittest

import listed_combo_fair as lcf

NOW = 1795440000.0          # 2026-11-23T13:20:00Z, a fixed clock (the model reads T from it)

IRAN_CELLS = {
    "DEAL-LT60": "Iran deal: Agreed, WTI: Below $60",
    "DEAL-60-74": "Iran deal: Agreed, WTI: $60 to $74.99",
    "DEAL-75-89": "Iran deal: Agreed, WTI: $75 to $89.99",
    "DEAL-GE90": "Iran deal: Agreed, WTI: $90 or above",
    "NODEAL-LT60": "Iran deal: Not agreed, WTI: Below $60",
    "NODEAL-60-74": "Iran deal: Not agreed, WTI: $60 to $74.99",
    "NODEAL-75-89": "Iran deal: Not agreed, WTI: $75 to $89.99",
    "NODEAL-GE90": "Iran deal: Not agreed, WTI: $90 or above",
}
INX_CELLS = {
    f"{p[:3].upper()}-{k}": f"Party: {p}, S&P 500 change: {v}"
    for p in ("Democratic", "Republican")
    for k, v in (("LTN10", "Below -10%"), ("N10-0", "-10% to 0%"), ("0-10", "0% to 10%"),
                 ("10-20", "10% to 20%"), ("GE20", "20% or above"))
}


class FakeKalshi:
    """get_json(path, params): an event's markets, and single leg markets."""

    def __init__(self, cells_by_event, legs):
        self.cells = cells_by_event
        self.legs = legs              # ticker -> (bid, ask) dollars, or an Exception
        self.calls = []

    def __call__(self, path, params):
        self.calls.append((path, dict(params or {})))
        if path == "/markets":
            ev = params["event_ticker"]
            return {"markets": [
                {"ticker": f"{ev}-{k}", "event_ticker": ev, "yes_sub_title": title,
                 "status": "active", "close_time": "2027-12-31T19:30:00Z"}
                for k, title in self.cells.get(ev, {}).items()]}
        mk = path.split("/")[-1]
        v = self.legs[mk]
        if isinstance(v, Exception):
            raise v
        return {"market": {"ticker": mk, "status": "active",
                           "yes_bid_dollars": f"{v[0]:.4f}", "yes_ask_dollars": f"{v[1]:.4f}"}}


def fake_yahoo(prices):
    def http(url, timeout=15.0):
        sym = url.split("/chart/")[1].split("?")[0].replace("%3D", "=")
        if sym not in prices:
            raise ValueError("404")
        return {"chart": {"result": [{"meta": {"regularMarketPrice": prices[sym],
                                               "regularMarketTime": NOW - 600}}]}}
    return http


class TestBins(unittest.TestCase):
    def test_parse_bin_reads_each_wording(self):
        self.assertEqual(lcf.parse_bin("Below $60"), (None, 60.0))
        self.assertEqual(lcf.parse_bin("$60 to $74.99"), (60.0, 74.99))
        self.assertEqual(lcf.parse_bin("$90 or above"), (90.0, None))
        self.assertEqual(lcf.parse_bin("1.9% or below"), (None, 1.9))
        self.assertEqual(lcf.parse_bin("4.0% or above"), (4.0, None))
        self.assertEqual(lcf.parse_bin("-10% to 0%"), (-10.0, 0.0))
        self.assertEqual(lcf.parse_bin("Below -10%"), (None, -10.0))
        self.assertEqual(lcf.parse_bin("-3% to -1%"), (-3.0, -1.0))
        self.assertEqual(lcf.parse_bin("−3% to −1%"), (-3.0, -1.0))     # a real minus
        self.assertEqual(lcf.parse_bin("3% or above"), (3.0, None))
        self.assertEqual(lcf.parse_bin("$3.40 to $3.79"), (3.4, 3.79))
        self.assertIsNone(lcf.parse_bin("Marine Le Pen"))
        self.assertIsNone(lcf.parse_bin("10% to 0%"))             # backwards: no range

    def test_bins_split_at_the_facing_edges(self):
        wti = {t: lcf.parse_bin(t) for t in ("Below $60", "$60 to $74.99", "$75 to $89.99",
                                             "$90 or above")}
        e = lcf.bin_edges(wti)
        self.assertEqual(e["Below $60"], (-math.inf, 60.0))
        self.assertAlmostEqual(e["$60 to $74.99"][1], 74.995)
        self.assertAlmostEqual(e["$75 to $89.99"][0], 74.995)
        self.assertEqual(e["$90 or above"][1], math.inf)
        cpi = {t: lcf.parse_bin(t) for t in ("1.9% or below", "2.0% to 2.9%", "3.0% to 3.9%",
                                             "4.0% or above")}
        e = lcf.bin_edges(cpi)
        self.assertAlmostEqual(e["1.9% or below"][1], 1.95)      # one-decimal print, inclusive
        self.assertAlmostEqual(e["2.0% to 2.9%"][1], 2.95)
        self.assertAlmostEqual(e["4.0% or above"][0], 3.95)
        inx = {t: lcf.parse_bin(t) for t in ("Below -10%", "-10% to 0%", "0% to 10%")
               + ("10% to 20%", "20% or above")}
        e = lcf.bin_edges(inx)
        self.assertEqual(e["-10% to 0%"], (-10.0, 0.0))          # lower bound in, upper out
        self.assertEqual(e["0% to 10%"], (0.0, 10.0))

    def test_a_grid_that_does_not_tile_fails(self):
        with self.assertRaises(ValueError):                      # a gap
            lcf.bin_edges({"Below $60": (None, 60.0), "$70 or above": (70.0, None)})
        with self.assertRaises(ValueError):                      # overlap
            lcf.bin_edges({"Below $60": (None, 60.0), "$50 or above": (50.0, None)})
        with self.assertRaises(ValueError):                      # a closed end
            lcf.bin_edges({"$0 to $60": (0.0, 60.0), "$60 or above": (60.0, None)})

    def test_split_title_either_order(self):
        self.assertEqual(lcf.split_title("Iran deal: Agreed, WTI: $60 to $74.99", "Iran deal", "WTI"),
                         ("Agreed", "$60 to $74.99"))
        self.assertEqual(lcf.split_title("WTI: Below $60, Iran deal: Not agreed", "Iran deal", "WTI"),
                         ("Not agreed", "Below $60"))
        self.assertIsNone(lcf.split_title("Agreed + Below $60", "Iran deal", "WTI"))


class TestModel(unittest.TestCase):
    NUM = {"model": "lognormal"}

    def test_a_grid_sums_to_its_outcomes(self):
        cfg = lcf.EVENTS["KXIRANWTICOMBO-28JAN"]
        edges = lcf.bin_edges({t: lcf.parse_bin(t) for t in
                               ("Below $60", "$60 to $74.99", "$75 to $89.99", "$90 or above")})
        cells = [{"ticker": f"{o}|{b}", "outcome": o, "edges": edges[b]}
                 for o in ("Agreed", "Not agreed") for b in edges]
        legs = {"KXUSAIRANAGREEMENT-27-28": {"bid": 0.26, "ask": 0.27, "mid": 0.265}}
        out = lcf.grid_fair(cells, cfg["binary"], legs, cfg["number"], {"fwd": 71.54},
                            cfg["delta"], cfg["delta_band"], NOW)
        self.assertAlmostEqual(sum(v["p"] for v in out.values()), 1.0, places=3)
        deal = sum(v["p"] for k, v in out.items() if k.startswith("Agreed|"))
        self.assertAlmostEqual(deal, 0.265, places=3)            # the deal market's mid
        for v in out.values():
            self.assertLessEqual(v["p_lo"], v["p"])
            self.assertLessEqual(v["p"], v["p_hi"])

    def test_the_link_moves_the_deal_world_down(self):
        """A deal lowers WTI (DELTA -8%): more of the deal world's mass sits under $60 than
        with no link, and less over $90."""
        cfg = lcf.EVENTS["KXIRANWTICOMBO-28JAN"]
        edges = lcf.bin_edges({t: lcf.parse_bin(t) for t in ("Below $60", "$60 to $74.99",
                                                             "$75 to $89.99", "$90 or above")})
        cells = [{"ticker": f"{o}|{b}", "outcome": o, "edges": edges[b]}
                 for o in ("Agreed", "Not agreed") for b in edges]
        legs = {"KXUSAIRANAGREEMENT-27-28": {"bid": 0.26, "ask": 0.27, "mid": 0.265}}
        linked = lcf.grid_fair(cells, cfg["binary"], legs, cfg["number"], {"fwd": 71.54},
                               -0.08, (-0.20, 0.0), NOW)
        flat = lcf.grid_fair(cells, cfg["binary"], legs, cfg["number"], {"fwd": 71.54},
                             0.0, (0.0, 0.0), NOW)
        self.assertGreater(linked["Agreed|Below $60"]["p"], flat["Agreed|Below $60"]["p"])
        self.assertLess(linked["Agreed|$90 or above"]["p"], flat["Agreed|$90 or above"]["p"])
        # ...and the no-deal world the other way, so the forward still holds overall
        self.assertLess(linked["Not agreed|Below $60"]["p"], flat["Not agreed|Below $60"]["p"])

    def test_independent_cells_are_products(self):
        num = {"model": "normal", "mean": 7.0, "mean_band": (7.0, 7.0), "sd": 16.6,
               "sd_band": (16.6, 16.6)}
        binary = lcf.EVENTS["KXINXPARTYCOMBO-30JAN"]["binary"]
        legs = {"KXPRESPARTY-2028-D": {"bid": 0.67, "ask": 0.68, "mid": 0.675},
                "KXPRESPARTY-2028-R": {"bid": 0.32, "ask": 0.33, "mid": 0.325}}
        cells = [{"ticker": "D", "outcome": "Democratic", "edges": (0.0, 10.0)},
                 {"ticker": "R", "outcome": "Republican", "edges": (0.0, 10.0)}]
        out = lcf.grid_fair(cells, binary, legs, num, None, 0.0, (0.0, 0.0), NOW)
        pb = lcf._ncdf((10 - 7) / 16.6) - lcf._ncdf((0 - 7) / 16.6)
        self.assertAlmostEqual(out["D"]["p"], 0.675 * pb, places=4)
        self.assertAlmostEqual(out["R"]["p"], 0.325 * pb, places=4)
        # the band: only the leg's own bid / ask left to move
        self.assertAlmostEqual(out["D"]["p_lo"], 0.67 * pb, places=4)
        self.assertAlmostEqual(out["D"]["p_hi"], 0.68 * pb, places=4)


class TestWatch(unittest.TestCase):
    EV = "KXIRANWTICOMBO-28JAN"

    def _watch(self, legs=None, cells=None, prices=None, events=None):
        legs = legs or {"KXUSAIRANAGREEMENT-27-28": (0.26, 0.27)}
        k = FakeKalshi(cells or {self.EV: IRAN_CELLS}, legs)
        w = lcf.ListedComboWatch(k, lambda: events or [self.EV],
                                 http=fake_yahoo(prices or {"CLG28.NYM": 71.54, "CL=F": 86.86}))
        return w, k

    def test_prices_the_grid_and_names_the_series(self):
        w, _k = self._watch()
        snap = w.refresh(NOW)
        ev = snap["events"][self.EV]
        self.assertEqual((ev["n"], ev["n_priced"], ev["err"]), (8, 8, None))
        self.assertAlmostEqual(ev["sum_p"], 1.0, places=2)
        self.assertEqual(snap["priced_series"], ["KXIRANWTICOMBO"])
        m = snap["markets"][f"{self.EV}-NODEAL-LT60"]
        self.assertEqual((m["outcome"], m["bin"], m["cut"]), ("Not agreed", "Below $60", False))
        self.assertLess(m["p_lo"], m["p"])
        self.assertLess(m["p"], m["p_hi"])
        self.assertEqual(m["ts"], NOW)
        self.assertEqual(snap["legs"][f"fut {self.EV}"]["symbol"], "CLG28.NYM")

    def test_the_forward_falls_back_to_a_neighbour_contract(self):
        w, _k = self._watch(prices={"CLF28.NYM": 72.21, "CL=F": 86.86})
        snap = w.refresh(NOW)
        self.assertEqual(snap["legs"][f"fut {self.EV}"]["symbol"], "CLF28.NYM")
        self.assertEqual(snap["events"][self.EV]["n_priced"], 8)

    def test_an_unmapped_event_is_named_not_priced(self):
        w, _k = self._watch(events=[self.EV, "KXNEWCOMBO-28MAR"])
        snap = w.refresh(NOW)
        self.assertEqual(snap["unmapped"], ["KXNEWCOMBO-28MAR"])
        self.assertIn("no leg map", snap["events"]["KXNEWCOMBO-28MAR"]["err"])
        self.assertNotIn("KXNEWCOMBO", snap["priced_series"])

    def test_a_leg_never_read_fails_the_grid_closed(self):
        w, _k = self._watch(legs={"KXUSAIRANAGREEMENT-27-28": (0.10, 0.40)})   # 30c wide
        snap = w.refresh(NOW)
        ev = snap["events"][self.EV]
        self.assertIn("no read", ev["err"])
        self.assertEqual(ev["n_priced"], 0)
        self.assertTrue(all("err" in m for m in snap["markets"].values()))
        self.assertEqual(snap["priced_series"], [])

    def test_a_failed_leg_keeps_its_last_read_and_ages(self):
        w, k = self._watch()
        w.refresh(NOW)
        k.legs["KXUSAIRANAGREEMENT-27-28"] = RuntimeError("timeout")
        snap = w.refresh(NOW + 120)
        m = snap["markets"][f"{self.EV}-DEAL-LT60"]
        self.assertEqual(m["ts"], NOW)                # the gate ages it out by its TTL
        self.assertIn("timeout", snap["legs"]["KXUSAIRANAGREEMENT-27-28"]["err"])

    def test_a_headline_move_holds_the_event(self):
        w, k = self._watch()
        w.refresh(NOW)
        k.legs["KXUSAIRANAGREEMENT-27-28"] = (0.30, 0.31)     # 26.5 -> 30.5: 4c
        snap = w.refresh(NOW + 60)
        self.assertEqual(snap["events"][self.EV]["hold_until"], NOW + 60 + lcf.LEG_HOLD_S)
        self.assertEqual(snap["markets"][f"{self.EV}-DEAL-LT60"]["hold_until"],
                         NOW + 60 + lcf.LEG_HOLD_S)
        w2, k2 = self._watch()
        w2.refresh(NOW)
        k2.legs["KXUSAIRANAGREEMENT-27-28"] = (0.28, 0.29)    # 2c: no hold
        self.assertIsNone(w2.refresh(NOW + 60)["events"][self.EV]["hold_until"])

    def test_a_front_month_jump_holds_the_wti_combo(self):
        w, _k = self._watch()
        w.refresh(NOW)
        w.http = fake_yahoo({"CLG28.NYM": 70.0, "CL=F": 84.5})   # -2.7%
        snap = w.refresh(NOW + lcf.FUT_REFRESH_S)
        self.assertEqual(snap["events"][self.EV]["hold_until"],
                         NOW + lcf.FUT_REFRESH_S + lcf.FUT_HOLD_S)

    def test_cut_within_a_day_of_a_leg_decision(self):
        w, _k = self._watch()
        dec = lcf._parse_iso(lcf.EVENTS[self.EV]["number"]["decides"]).timestamp()
        snap = w.refresh(dec - 23 * 3600)
        self.assertTrue(snap["markets"][f"{self.EV}-DEAL-LT60"]["cut"])
        w2, _k2 = self._watch()
        self.assertFalse(w2.refresh(NOW)["markets"][f"{self.EV}-DEAL-LT60"]["cut"])

    def test_a_missing_cell_fails_the_grid(self):
        cells = dict(IRAN_CELLS)
        del cells["NODEAL-GE90"]
        w, _k = self._watch(cells={self.EV: cells})
        snap = w.refresh(NOW)
        self.assertIn("cells", snap["events"][self.EV]["err"])
        self.assertEqual(snap["events"][self.EV]["n_priced"], 0)

    def test_a_cell_title_that_does_not_parse_fails_the_grid(self):
        cells = dict(IRAN_CELLS)
        cells["DEAL-LT60"] = "Agreed + Below $60"
        w, _k = self._watch(cells={self.EV: cells})
        self.assertIn("legs not", w.refresh(NOW)["events"][self.EV]["err"])

    def test_the_gas_grid_prices_the_pump_off_rbob_plus_the_spread(self):
        """Jack 2026-11-17: "allow gas markets on combos". KXHORMUZGASCOMBO: Hormuz traffic
        (KXHORMUZNORM's Before Jul 1, 2027, 36/37) x the Jul 31, 2027 pump price = Aug-2027
        RBOB $2.61 + $1.00."""
        ev = "KXHORMUZGASCOMBO-27AUG"
        cells = {f"{o[0]}-{k}": f"Hormuz traffic: {o[1]}, Gas price: {v}"
                 for o in (("BACK", "Back to normal"), ("NOT", "Not back to normal"))
                 for k, v in (("LT3.40", "Below $3.40"), ("3.40-3.79", "$3.40 to $3.79"),
                              ("3.80-4.19", "$3.80 to $4.19"), ("GE4.20", "$4.20 or above"))}
        k = FakeKalshi({ev: cells}, {"KXHORMUZNORM-26MAR17-B270701": (0.36, 0.37)})
        w = lcf.ListedComboWatch(k, lambda: [ev],
                                 http=fake_yahoo({"RBQ27.NYM": 2.6119, "RB=F": 2.5277}))
        snap = w.refresh(NOW)
        e = snap["events"][ev]
        self.assertEqual((e["n"], e["n_priced"], e["err"]), (8, 8, None))
        self.assertAlmostEqual(e["sum_p"], 1.0, places=2)
        back = sum(m["p"] for t, m in snap["markets"].items() if "-BACK-" in t)
        self.assertAlmostEqual(back, 0.365, places=3)
        p = {t.split(ev + "-")[1]: m["p"] for t, m in snap["markets"].items()}
        # the 'back' world sits lower (DELTA -6%) than the 'not back' world, against $3.61
        back_low = p["BACK-LT3.40"] / 0.365
        not_low = p["NOT-LT3.40"] / 0.635
        self.assertGreater(back_low, not_low)
        self.assertGreater(p["NOT-GE4.20"] / 0.635, p["BACK-GE4.20"] / 0.365)
        self.assertEqual(snap["priced_series"], ["KXHORMUZGASCOMBO"])

    def test_the_party_grid_reads_both_party_markets(self):
        ev = "KXINXPARTYCOMBO-30JAN"
        legs = {"KXPRESPARTY-2028-D": (0.67, 0.68), "KXPRESPARTY-2028-R": (0.32, 0.33)}
        k = FakeKalshi({ev: INX_CELLS}, legs)
        w = lcf.ListedComboWatch(k, lambda: [ev], http=fake_yahoo({}))
        snap = w.refresh(NOW)
        e = snap["events"][ev]
        self.assertEqual((e["n"], e["n_priced"], e["err"]), (10, 10, None))
        rep = sum(m["p"] for t, m in snap["markets"].items() if "-REP-" in t)
        self.assertAlmostEqual(rep, 0.325, places=3)
        self.assertEqual(snap["priced_series"], ["KXINXPARTYCOMBO"])
        self.assertEqual(e["legs"], ["KXPRESPARTY-2028-D", "KXPRESPARTY-2028-R"])


if __name__ == "__main__":
    unittest.main()
