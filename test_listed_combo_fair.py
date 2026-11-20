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
        self.assertEqual(lcf.parse_bin("Below 0"), (None, 0.0))                 # jobs added
        self.assertEqual(lcf.parse_bin("0 to 500,000"), (0.0, 500000.0))
        self.assertEqual(lcf.parse_bin("500,000 to 1 million"), (500000.0, 1000000.0))
        self.assertEqual(lcf.parse_bin("1 million to 1.5 million"), (1000000.0, 1500000.0))
        self.assertEqual(lcf.parse_bin("1.5 million or above"), (1500000.0, None))
        self.assertEqual(lcf.parse_bin("4.1% to 4.5%"), (4.1, 4.5))
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

    def test_a_short_title_reads_the_cells_own_rules(self):
        """Since 11/19 Kalshi titles the party grids' cells short ("Democrat, 500K-1M"): the
        legs come from the cell's own rules."""
        m = {"yes_sub_title": "Democrat, 500K-1M",
             "rules_primary": "If ALL of the following occur: Party: Democratic, Jobs added in "
                              "2029: 500,000 to 1 million, then the market resolves to Yes."}
        self.assertEqual(lcf.cell_split(m, "Party", "Jobs added in 2029"),
                         ("Democratic", "500,000 to 1 million"))
        fed = {"yes_sub_title": "No change, 0",
               "rules_primary": "If ALL of the following occur for Dec 2026: Federal Funds Rate "
                                "Decision: No change, Dissents: 0, then the market resolves to Yes."}
        self.assertEqual(lcf.cell_split(fed, "Federal Funds Rate Decision", "Dissents"),
                         ("No change", "0"))
        # the title still goes first
        self.assertEqual(lcf.cell_split({"yes_sub_title": "Iran deal: Agreed, WTI: Below $60",
                                         "rules_primary": "?"}, "Iran deal", "WTI"),
                         ("Agreed", "Below $60"))
        self.assertIsNone(lcf.cell_split({"yes_sub_title": "Democrat, 0-500K"}, "Party",
                                         "Jobs added in 2029"))


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
        self.assertIn("unmapped: no open markets", snap["events"]["KXNEWCOMBO-28MAR"]["err"])
        self.assertEqual(snap["unmapped_why"], {"KXNEWCOMBO-28MAR": "no open markets"})
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


import json
import os

# the live grids' own rules (11/17 public reads) and their cells
RULES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "test_listed_combo_fair_rules.json"), encoding="utf-8"))


def _grid(binary_label, outcomes, number_label, bins):
    return {f"{o[:3].upper()}{i}-{j}": f"{binary_label}: {o}, {number_label}: {b}"
            for i, o in enumerate(outcomes) for j, b in enumerate(bins)}


def _party_grid(number_label, bins):
    """A party grid as Kalshi lists it since 11/19: a short title ("Democrat, 500K-1M") and the
    legs only in each cell's own rules. bins: (short, as the rules write it)."""
    return {f"{p[:3].upper()}{i}-{j}": (f"{short}, {s}", f"Party: {p}, {number_label}: {b}")
            for i, (p, short) in enumerate((("Democratic", "Democrat"), ("Republican", "Republican")))
            for j, (s, b) in enumerate(bins)}


GRIDS = {
    "KXIRANWTICOMBO-28JAN": _grid("Iran deal", ("Agreed", "Not agreed"), "WTI",
                                  ("Below $60", "$60 to $74.99", "$75 to $89.99", "$90 or above")),
    "KXHORMUZGASCOMBO-27AUG": _grid("Hormuz traffic", ("Back to normal", "Not back to normal"),
                                    "Gas price", ("Below $3.40", "$3.40 to $3.79",
                                                  "$3.80 to $4.19", "$4.20 or above")),
    "KXINXPARTYCOMBO-30JAN": _party_grid("S&P 500 change", (
        ("below -10%", "Below -10%"), ("-10% to 0%", "-10% to 0%"), ("0-10%", "0% to 10%"),
        ("10-20%", "10% to 20%"), ("20% or above", "20% or above"))),
    "KXCPIPARTYCOMBO-30MAR": _party_grid("CPI inflation", (
        ("1.9% or below", "1.9% or below"), ("2.0-2.9%", "2.0% to 2.9%"),
        ("3.0-3.9%", "3.0% to 3.9%"), ("4.0% or above", "4.0% or above"))),
    "KXGDPPARTYCOMBO-30MAR": _party_grid("GDP growth", (
        ("0.0% or below", "0.0% or below"), ("0.1-1.0%", "0.1% to 1.0%"),
        ("1.1-2.0%", "1.1% to 2.0%"), ("2.1-2.5%", "2.1% to 2.5%"), ("2.6-3.0%", "2.6% to 3.0%"),
        ("3.1% or above", "3.1% or above"))),
    "KXLEPENEURCOMBO-27JUN": _grid("French president", ("Marine Le Pen", "Not Marine Le Pen"),
                                   "EUR/USD change", ("Below -3%", "-3% to -1%", "-1% to 1%",
                                                      "1% to 3%", "3% or above")),
    "KXPAYROLLSPARTYCOMBO-30SEP": _party_grid("Jobs added in 2029", (
        ("below 0", "Below 0"), ("0-500K", "0 to 500,000"), ("500K-1M", "500,000 to 1 million"),
        ("1M-1.5M", "1 million to 1.5 million"), ("1.5M or above", "1.5 million or above"))),
    "KXU3PARTYCOMBO-30SEP": _party_grid("Unemployment rate", (
        ("4.0% or below", "4.0% or below"), ("4.1-4.5%", "4.1% to 4.5%"),
        ("4.6-5.0%", "4.6% to 5.0%"), ("5.1% or above", "5.1% or above"))),
}
LEG_LISTS = {
    ("event_ticker", "KXPRESPARTY-2028"): [("KXPRESPARTY-2028-D", "Democratic party"),
                                           ("KXPRESPARTY-2028-R", "Republican party")],
    ("event_ticker", "KXFRENCHPRES-27"): [("KXFRENCHPRES-27-MLEP", "Marine Le Pen"),
                                          ("KXFRENCHPRES-27-JBAR", "Jordan Bardella")],
    ("series_ticker", "KXUSAIRANAGREEMENT"): [("KXUSAIRANAGREEMENT-27", "Before Jan 1, 2027"),
                                              ("KXUSAIRANAGREEMENT-27-28", "Before Jan 1, 2028")],
    ("series_ticker", "KXHORMUZNORM"): [("KXHORMUZNORM-26MAR17-B270601", "Before Jun 1, 2027"),
                                        ("KXHORMUZNORM-26MAR17-B270701", "Before Jul 1, 2027")],
}
LEG_BOOKS = {"KXPRESPARTY-2028-D": (0.67, 0.68), "KXPRESPARTY-2028-R": (0.32, 0.33),
             "KXFRENCHPRES-27-MLEP": (0.52, 0.53), "KXUSAIRANAGREEMENT-27-28": (0.26, 0.27),
             "KXHORMUZNORM-26MAR17-B270701": (0.36, 0.37)}
FUT = {"CLG28.NYM": 71.54, "CL=F": 86.86, "RBQ27.NYM": 2.6119, "RB=F": 2.5277}


class FakeKalshiFull(FakeKalshi):
    """The combo grids with their rules, plus the leg events / series the auto-mapper reads."""

    def __init__(self, grids=None, rules=None, legs=None):
        super().__init__({}, dict(LEG_BOOKS if legs is None else legs))
        self.grids, self.rules = GRIDS if grids is None else grids, RULES if rules is None else rules

    def __call__(self, path, params):
        self.calls.append((path, dict(params or {})))
        if path == "/markets":
            for key in ("event_ticker", "series_ticker"):
                if (key, params.get(key)) in LEG_LISTS:
                    return {"markets": [{"ticker": t, "yes_sub_title": s, "status": "active"}
                                        for t, s in LEG_LISTS[(key, params[key])]]}
            ev = params.get("event_ticker")
            r = self.rules.get(ev) or {}
            return {"markets": [
                {"ticker": f"{ev}-{k}", "event_ticker": ev, "status": "active",
                 "close_time": "2029-12-31T21:00:00Z", **r, **self._cell(title)}
                for k, title in (self.grids.get(ev) or {}).items()]}
        return super().__call__(path, params)

    @staticmethod
    def _cell(title):
        """A long title as is; a (short title, legs) pair puts the legs in the cell's own rules."""
        if isinstance(title, tuple):
            return {"yes_sub_title": title[0], "rules_primary":
                    f"If ALL of the following occur: {title[1]}, then the market resolves to Yes."}
        return {"yes_sub_title": title}


class TestAutoMap(unittest.TestCase):
    """AUTO-MAPPING (Jack 2026-11-17: "Yes, build it"): a combo event with no hand row is mapped
    from its own rules when both legs are kinds already priced; the eight live grids, read with
    no hand rows at all, map to exactly the hand rows' legs."""

    def test_every_live_grid_maps_to_the_hand_rows_legs(self):
        k = FakeKalshiFull()
        for ev in GRIDS:
            mk = k("/markets", {"event_ticker": ev})["markets"]
            a = lcf.auto_map(ev, mk, k, NOW)
            h = lcf.EVENTS[ev]
            self.assertEqual({o: tuple(v) for o, v in a["binary"]["outcomes"].items()},
                             {o: tuple(v) for o, v in h["binary"]["outcomes"].items()}, ev)
            self.assertEqual(a["binary"]["yes"], h["binary"]["yes"], ev)
            self.assertEqual(a["number"]["model"], h["number"]["model"], ev)
            self.assertEqual((a["delta"], tuple(a["delta_band"])),
                             (h["delta"], tuple(h["delta_band"])), ev)
            if h["number"]["model"] == "lognormal":
                self.assertEqual(a["number"]["fwd"][0], h["number"]["fwd"][0], ev)
                self.assertEqual(a["number"].get("fwd_add"), h["number"].get("fwd_add"), ev)
            else:
                self.assertAlmostEqual(a["number"]["sd"], h["number"]["sd"], delta=0.1, msg=ev)
                self.assertAlmostEqual(a["number"]["mean"], h["number"]["mean"], delta=0.4, msg=ev)

    def test_auto_and_hand_price_the_same(self):
        evs = sorted(GRIDS)
        hand = lcf.ListedComboWatch(FakeKalshiFull(), lambda: evs, http=fake_yahoo(FUT))
        auto = lcf.ListedComboWatch(FakeKalshiFull(), lambda: evs, http=fake_yahoo(FUT),
                                    event_map={})
        sh, sa = hand.refresh(NOW), auto.refresh(NOW)
        self.assertEqual(sa["unmapped"], [])
        self.assertEqual(sorted(sa["auto"]), evs)
        self.assertEqual({e["mapping"] for e in sa["events"].values()}, {"auto"})
        self.assertEqual({e["mapping"] for e in sh["events"].values()}, {"hand"})
        self.assertEqual(sa["priced_series"], sh["priced_series"])
        for t, m in sh["markets"].items():
            self.assertAlmostEqual(sa["markets"][t]["p"], m["p"], delta=0.006, msg=t)

    def test_a_new_date_of_a_known_grid_maps_itself(self):
        """Another Iran x WTI expiry -- no hand row -- reads its own deal market and contract."""
        ev = "KXIRANWTICOMBO-28JUL"
        rules = {"rules_secondary": RULES["KXIRANWTICOMBO-28JAN"]["rules_secondary"]
                 .replace("Before Jan 1, 2028", "Before Jan 1, 2027")
                 .replace("Dec 31, 2027 at 2:30 PM ET", "Jun 30, 2027 at 2:30 PM ET")
                 .replace("February 2028 contract", "August 2027 contract")}
        k = FakeKalshiFull(grids={ev: GRIDS["KXIRANWTICOMBO-28JAN"]}, rules={ev: rules},
                           legs=dict(LEG_BOOKS, **{"KXUSAIRANAGREEMENT-27": (0.04, 0.05)}))
        w = lcf.ListedComboWatch(k, lambda: [ev], http=fake_yahoo({"CLQ27.NYM": 74.0, "CL=F": 86.86}))
        snap = w.refresh(NOW)
        e = snap["events"][ev]
        self.assertEqual((e["mapping"], e["n_priced"], e["err"]), ("auto", 8, None))
        a = snap["auto"][ev]
        self.assertEqual(a["binary"]["Agreed"], ["KXUSAIRANAGREEMENT-27", True])
        self.assertEqual(a["number"]["fwd"][0], "CLQ27.NYM")
        self.assertEqual(a["number"]["at"], "2027-06-30T18:30:00Z")
        deal = sum(m["p"] for t, m in snap["markets"].items() if "-AGR" in t)
        self.assertAlmostEqual(deal, 0.045, places=3)

    def test_what_cannot_be_mapped_says_why(self):
        k = FakeKalshiFull()
        # two yes/no legs (the Fed decision x dissents)
        fed = [{"ticker": "KXFEDCOMBO-26DEC-0-0", "yes_sub_title": "Rate: No change, Dissents: 0",
                "status": "active", **RULES["KXFEDCOMBO-26DEC"]}]
        with self.assertRaisesRegex(ValueError, "not one yes/no leg and one number"):
            lcf.auto_map("KXFEDCOMBO-26DEC", fed, k, NOW)
        # an S&P window already running
        inx = k("/markets", {"event_ticker": "KXINXPARTYCOMBO-30JAN"})["markets"]
        late = lcf._parse_iso("2029-01-10T00:00:00Z").timestamp()
        with self.assertRaisesRegex(ValueError, "within 30 days"):
            lcf.auto_map("KXINXPARTYCOMBO-30JAN", inx, k, late)
        # a leg market that is not listed
        iran = [dict(m, rules_secondary=m["rules_secondary"].replace("Before Jan 1, 2028",
                                                                     "Before Jan 1, 2029"))
                for m in k("/markets", {"event_ticker": "KXIRANWTICOMBO-28JAN"})["markets"]]
        with self.assertRaisesRegex(ValueError, "no KXUSAIRANAGREEMENT market 'Before Jan 1, 2029'"):
            lcf.auto_map("KXIRANWTICOMBO-28JAN", iran, k, NOW)
        # a ruleset nothing prices
        odd = [dict(m, rules_secondary=m["rules_secondary"].replace("(AGREEMENT)", "(TREATY)"))
               for m in iran]
        with self.assertRaisesRegex(ValueError, r"no pricer for the yes/no leg 'Iran deal' \(TREATY\)"):
            lcf.auto_map("KXIRANWTICOMBO-28JAN", odd, k, NOW)
        with self.assertRaisesRegex(ValueError, "no two Kalshi rulesets"):
            lcf.auto_map("X", [{"yes_sub_title": "A: b, C: 1% to 2%", "rules_secondary": "?"}], k, NOW)

    def test_the_watch_names_an_unmappable_event_and_a_hand_row_wins(self):
        ev = "KXFEDCOMBO-26DEC"
        grids = dict(GRIDS, **{ev: {"0-0": "Rate: No change, Dissents: 0",
                                    "0-T0": "Rate: No change, Dissents: 1 or more"}})
        k = FakeKalshiFull(grids=grids, rules=dict(RULES))
        w = lcf.ListedComboWatch(k, lambda: [ev, "KXIRANWTICOMBO-28JAN"], http=fake_yahoo(FUT))
        snap = w.refresh(NOW)
        self.assertEqual(snap["unmapped"], [ev])
        self.assertIn("not one yes/no leg", snap["unmapped_why"][ev])
        d = snap["unmapped_detail"][ev]
        self.assertEqual(d["cells"], 2)
        self.assertEqual(d["components"], ["Federal Funds Rate Decision (FEDDECISION)",
                                           "Dissents (FOMCDISSENTCOUNT)"])
        self.assertEqual(snap["events"]["KXIRANWTICOMBO-28JAN"]["mapping"], "hand")
        self.assertNotIn("KXIRANWTICOMBO-28JAN", snap["auto"])
        # an unmappable event is retried only after META_REFRESH_S
        n = len(k.calls)
        w.refresh(NOW + 30)
        self.assertFalse(any(p == "/markets" and q.get("series_ticker") for p, q in k.calls[n:]))


class TestJobsGrids(unittest.TestCase):
    """The payrolls and U-3 party grids (Jack 2026-11-20: "Yes, map them"): short titles, the
    legs in each cell's own rules, bins in jobs ("500,000 to 1 million") or one-decimal rates."""

    def _snap(self, ev):
        w = lcf.ListedComboWatch(FakeKalshiFull(), lambda: [ev], http=fake_yahoo({}))
        return w.refresh(NOW)

    def _edges(self, got, want):
        self.assertEqual([x is None for x in got], [x is None for x in want])
        for g, x in zip(got, want):
            if x is not None:
                self.assertAlmostEqual(g, x, places=9)

    def test_the_payrolls_grid_prices_off_the_cells_rules(self):
        ev = "KXPAYROLLSPARTYCOMBO-30SEP"
        snap = self._snap(ev)
        e = snap["events"][ev]
        self.assertEqual((e["mapping"], e["n"], e["n_priced"], e["err"]), ("hand", 10, 10, None))
        self.assertAlmostEqual(e["sum_p"], 1.0, places=3)          # D 67.5 + R 32.5
        self.assertEqual(snap["priced_series"], ["KXPAYROLLSPARTYCOMBO"])
        m = {v["bin"]: v for v in snap["markets"].values() if v.get("outcome") == "Democratic"}
        self.assertEqual(sorted(m), sorted(["Below 0", "0 to 500,000", "500,000 to 1 million",
                                            "1 million to 1.5 million", "1.5 million or above"]))
        self._edges(m["Below 0"]["edges"], [None, 0.0])
        self._edges(m["500,000 to 1 million"]["edges"], [500000.0, 1000000.0])
        self._edges(m["1.5 million or above"]["edges"], [1500000.0, None])
        # P(D) x P(jobs < 0) under N(550K, 850K)
        self.assertAlmostEqual(m["Below 0"]["p"], 0.675 * lcf._ncdf(-550000 / 850000), places=4)
        self.assertAlmostEqual(sum(v["p"] for v in m.values()), 0.675, places=3)
        for v in snap["markets"].values():
            self.assertLess(v["p_lo"], v["p"])
            self.assertLess(v["p"], v["p_hi"])

    def test_the_u3_grid_splits_one_decimal_bins_at_the_half(self):
        ev = "KXU3PARTYCOMBO-30SEP"
        snap = self._snap(ev)
        e = snap["events"][ev]
        self.assertEqual((e["n"], e["n_priced"], e["err"]), (8, 8, None))
        m = {v["bin"]: v for v in snap["markets"].values() if v.get("outcome") == "Republican"}
        self._edges(m["4.0% or below"]["edges"], [None, 4.05])
        self._edges(m["4.1% to 4.5%"]["edges"], [4.05, 4.55])
        self._edges(m["5.1% or above"]["edges"], [5.05, None])
        self.assertAlmostEqual(sum(v["p"] for v in m.values()), 0.325, places=3)

    def test_a_payrolls_window_under_way_is_not_priced_off_the_prior(self):
        k = FakeKalshiFull()
        mk = k("/markets", {"event_ticker": "KXPAYROLLSPARTYCOMBO-30SEP"})["markets"]
        late = lcf._parse_iso("2028-12-15T00:00:00Z").timestamp()
        with self.assertRaisesRegex(ValueError, "under way"):
            lcf.auto_map("KXPAYROLLSPARTYCOMBO-30SEP", mk, k, late)


if __name__ == "__main__":
    unittest.main()
