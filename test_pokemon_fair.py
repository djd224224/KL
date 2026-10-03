"""Tests for pokemon_fair (the Pokemon gate's TCGplayer fair values)."""
import json
import math
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import pokemon_fair as pf


def rules(item, k, d="Oct 31, 2026", word="above"):
    return (f"If the Ungraded Price of the {item} on Collectr is {word} ${k} "
            f"on {d}, then the market resolves to Yes.")


CLOSE = "2026-11-01T03:59:00Z"                    # 23:59 ET Oct 31
# Saturday 2026-10-03 12:00Z, 28.66 days before the close
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
TAU = (datetime(2026, 11, 1, 3, 59, tzinfo=timezone.utc) - NOW
       ).total_seconds() / (pf.MONTH_DAYS * 86400)


def market(ev, item, k, close=CLOSE, strike_type="greater", word="above",
           d="Oct 31, 2026", ticker_k=None):
    return {"ticker": f"{ev}-{ticker_k if ticker_k is not None else k}",
            "event_ticker": ev, "floor_strike": float(k),
            "strike_type": strike_type, "close_time": close,
            "rules_primary": rules(item, k, d, word)}


CHA = market("KXPOKEMON-26OCTCHA", "Charizard", "194.14")
CHA_SPEC = {"item": "Charizard", "tcgplayer_id": 714372, "kind": "card",
            "printing": "Foil"}
# pricepoints as TCGplayer returned them on 2026-10-03
PP_CHA = [{"printingType": "Normal", "marketPrice": None, "listedMedianPrice": None},
          {"printingType": "Foil", "marketPrice": 171.77, "listedMedianPrice": 197.72}]
PP_BRG = [{"printingType": "Normal", "marketPrice": None, "listedMedianPrice": None},
          {"printingType": "Foil", "marketPrice": None, "listedMedianPrice": 5010.87}]
PP_ETB = [{"printingType": "Normal", "marketPrice": 295.73, "listedMedianPrice": 399.99},
          {"printingType": "Foil", "marketPrice": None, "listedMedianPrice": None}]


def price(v0, src="market", ts=None, moved_at=None):
    return {"v0": v0, "src": src, "market": v0 if src == "market" else None,
            "median": None, "printing": "Foil",
            "ts": NOW.timestamp() if ts is None else ts, "moved_at": moved_at}


class TestRules(unittest.TestCase):
    def test_the_october_rules_as_listed(self):
        live = {  # rules_primary of the seven October markets, 2026-10-03
            "Pikachu ex - 109 (30th Celebration)": "299.99", "Mew ex": "84.4",
            "Charizard": "194.14", "Mew - R/RGB": "6000.27",
            "Mew - G/RGB": "3738.75", "Mew - B/RGB": "5010.87",
            "30th Celebration Pokemon Center Elite Trainer Box": "313.44"}
        for item, k in live.items():
            r = pf.parse_rules(rules(item, k))
            self.assertEqual((r["item"], r["dir"], r["k"], r["date"]),
                             (item, "above", float(k), date(2026, 10, 31)), item)

    def test_other_shapes(self):
        r = pf.parse_rules(rules("Celebrations Ultra Premium Collection",
                                 "1,237.81", d="September 30, 2026"))
        self.assertEqual((r["k"], r["date"]), (1237.81, date(2026, 9, 30)))
        self.assertEqual(pf.parse_rules(rules("Squirtle", "27.82", word="below"))["dir"],
                         "below")
        self.assertIsNone(pf.parse_rules("If the Ungraded Price of the Squirtle on "
                                         "Collectr is above $27.82 on ?, then ..."))
        self.assertIsNone(pf.parse_rules(""))

    def test_item_names_compare_like_a_reader(self):
        self.assertEqual(pf.norm_item("Team Rocket’s  Moltres ex"),
                         pf.norm_item("team rocket's moltres EX"))
        self.assertNotEqual(pf.norm_item("Charizard"), pf.norm_item("Charmander"))


class TestModel(unittest.TestCase):
    def test_p_above(self):
        self.assertAlmostEqual(pf.p_above(100, 100, 1.0, 0.0, 0.2), 0.5, places=2)
        self.assertLess(pf.p_above(100, 100, 1.0, 0.0, 0.2), 0.5)  # strict, at cents
        self.assertLess(pf.p_above(100, 100, 1.0, -0.06, 0.18), 0.5)
        lo, hi = pf.p_above(90, 100, 1.0, 0, 0.2), pf.p_above(110, 100, 1.0, 0, 0.2)
        self.assertLess(lo, 0.5)
        self.assertGreater(hi, 0.5)
        self.assertAlmostEqual(lo + pf.p_above(100 * 100 / 90, 100, 1.0, 0, 0.2), 1.0,
                               delta=0.01)
        # no time left: the price is the answer
        self.assertEqual(pf.p_above(194.15, 194.14, 0.0, 0, 0.2), 1.0)
        self.assertEqual(pf.p_above(194.14, 194.14, 0.0, 0, 0.2), 0.0)

    def test_charizard_on_the_day(self):
        # TCGplayer 171.77 against the 194.14 strike, 28.7 days out: ~15c
        p = pf.p_above(171.77, 194.14, TAU, pf.MU_CARD, pf.SIGMA_CARD)
        self.assertAlmostEqual(p, 0.153, delta=0.01)

    def test_thin_items_keep_the_no_sale_mass(self):
        p, q = pf.p_thin(6000.27, 6000.27, TAU, -0.06, 0.25, 1.0)
        self.assertAlmostEqual(q, math.exp(-TAU), places=9)
        self.assertAlmostEqual(p, (1 - q) * pf.p_above(6000.27, 6000.27, TAU, -0.06, 0.25))
        self.assertLess(p, 0.3)
        p_up, _ = pf.p_thin(6100.0, 6000.27, TAU, -0.06, 0.25, 1.0)
        self.assertGreater(p_up, q)                   # an unmoved price above K wins
        p0, q0 = pf.p_thin(6000.27, 6000.27, TAU, -0.06, 0.25, 0.0)
        self.assertEqual((p0, q0), (0.0, 1.0))        # nothing ever sells


class TestPrice(unittest.TestCase):
    def test_collectr_reads_market_else_median(self):
        self.assertEqual(pf.pick_price(PP_CHA, "Foil")["v0"], 171.77)
        got = pf.pick_price(PP_BRG, "Foil")            # 26OCTMEWBRG's strike
        self.assertEqual((got["v0"], got["src"]), (5010.87, "median"))
        self.assertEqual(pf.pick_price(PP_ETB, "Normal")["v0"], 295.73)
        self.assertEqual(pf.pick_price(PP_CHA, None)["v0"], 171.77)   # one priced row

    def test_doubts_are_errors(self):
        self.assertIn("err", pf.pick_price(PP_CHA, "Normal"))     # no price there
        self.assertIn("err", pf.pick_price(PP_CHA, "Reverse Holofoil"))
        self.assertIn("err", pf.pick_price([], "Foil"))
        both = [dict(PP_CHA[1], printingType="Normal"), PP_CHA[1]]
        self.assertIn("err", pf.pick_price(both, None))           # ambiguous
        self.assertIn("err", pf.pick_price({"error": "x"}, "Foil"))


class TestEntries(unittest.TestCase):
    def test_liquid_card(self):
        e = pf.market_entry(CHA, CHA_SPEC, price(171.77), 71, NOW)
        self.assertEqual((e["pid"], e["kind"], e["thin"], e["src"]),
                         (714372, "card", False, "market"))
        self.assertAlmostEqual(e["p"], pf.p_above(171.77, 194.14, TAU, pf.MU_CARD,
                                                  pf.SIGMA_CARD))
        self.assertAlmostEqual(e["tau_m"], TAU, places=3)

    def test_thin_and_median_items(self):
        brg = market("KXPOKEMON-26OCTMEWBRG", "Mew - B/RGB", "5010.87")
        spec = {"item": "Mew - B/RGB", "tcgplayer_id": 717609, "kind": "card",
                "printing": "Foil"}
        e = pf.market_entry(brg, spec, price(5010.87, src="median"), 60, NOW)
        self.assertTrue(e["thin"])                    # median source, however many sellers
        self.assertEqual(e["sigma"], pf.THIN_SIGMA)
        self.assertIn("q", e)
        e = pf.market_entry(CHA, CHA_SPEC, price(171.77), 4, NOW)
        self.assertTrue(e["thin"])                    # 4 sellers

    def test_sealed_uses_the_sealed_walk(self):
        etb = market("KXPOKEMON-26OCT30TCELPO",
                     "30th Celebration Pokemon Center Elite Trainer Box", "313.44")
        spec = {"item": "30th Celebration Pokemon Center Elite Trainer Box",
                "tcgplayer_id": 704144, "kind": "sealed", "printing": "Normal"}
        e = pf.market_entry(etb, spec, price(295.73), 130, NOW)
        self.assertEqual((e["kind"], e["mu"], e["sigma"]),
                         ("sealed", pf.MU_SEALED, pf.SIGMA_SEALED))

    def test_every_doubt_is_an_error(self):
        ok = (CHA, CHA_SPEC, price(171.77), 71)
        def err(m=ok[0], spec=ok[1], pr=ok[2], sellers=ok[3]):
            return pf.market_entry(m, spec, pr, sellers, NOW).get("err")
        self.assertIsNone(err())
        self.assertIn("unmapped", err(spec=None))
        # the September code reuse: CHA was Charmander
        self.assertIn("rules name", err(m=market("KXPOKEMON-26OCTCHA", "Charmander", "39.87")))
        self.assertIn("strike mismatch", err(m=market("KXPOKEMON-26OCTCHA", "Charizard",
                                                      "194.14", ticker_k="194.15")))
        self.assertIn("rules date", err(m=market("KXPOKEMON-26OCTCHA", "Charizard",
                                                 "194.14", d="Oct 30, 2026")))
        self.assertIn("below", err(m=market("KXPOKEMON-26OCTCHA", "Charizard", "194.14",
                                            word="below")))
        self.assertIn("strike_type", err(m=market("KXPOKEMON-26OCTCHA", "Charizard",
                                                  "194.14", strike_type="less")))
        self.assertIn("rules:", err(m=dict(CHA, rules_primary="Will it rain?")))
        self.assertIn("no TCGplayer read", err(pr=None))
        self.assertIn("TCGplayer 714372", err(pr={"err": "no Foil printing on TCGplayer"}))
        self.assertIn("seller count", err(sellers=None))
        self.assertIn("check the mapping", err(pr=price(80.0)))   # 194.14 -> 80: x0.41
        self.assertIsNone(err(pr=price(120.0)))                    # -48% is a move


class FakeResp:
    def __init__(self, js, status=200):
        self.js, self.status_code = js, status

    def json(self):
        return self.js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """TCGplayer by URL: pricepoints[pid], details[pid] (sellers), search."""

    def __init__(self):
        self.pricepoints, self.sellers, self.search = {}, {}, []
        self.calls = []
        self.down = set()

    def get(self, url, timeout=None):
        self.calls.append(url)
        pid = int(url.split("/product/")[1].split("/")[0])
        if pid in self.down:
            raise RuntimeError("timed out")
        if url.endswith("/pricepoints"):
            return FakeResp(self.pricepoints[pid])
        return FakeResp({"sellers": self.sellers.get(pid)})

    def post(self, url, params=None, json=None, timeout=None):
        return FakeResp({"results": [{"results": self.search}]})


class TestWatch(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="poke_")
        self.map = os.path.join(self.dir, "products.json")
        self._write_map({"KXPOKEMON-26OCTCHA": dict(CHA_SPEC, verified="t")})
        self.family = [CHA, market("KXPOKEMON-26OCTMEWEX", "Mew ex", "84.4")]
        self.kalshi_calls = 0
        self.s = FakeSession()
        self.s.pricepoints[714372] = PP_CHA
        self.s.sellers[714372] = 71.0
        p = mock.patch.object(pf, "TCG_MIN_GAP_SECS", 0.0)
        p.start()
        self.addCleanup(p.stop)

    def _write_map(self, events):
        with open(self.map, "w", encoding="utf-8") as f:
            json.dump({"events": events}, f)

    def _get_json(self, path, params):
        self.kalshi_calls += 1
        self.assertEqual((path, params["series_ticker"], params["status"]),
                         ("/markets", "KXPOKEMON", "open"))
        return {"markets": self.family, "cursor": ""}

    def _watch(self):
        return pf.PokemonWatch(self._get_json, session=self.s, products_file=self.map)

    def test_refresh_end_to_end_without_the_network(self):
        w = self._watch()
        snap = w.refresh(NOW.timestamp())
        cha = snap["markets"]["KXPOKEMON-26OCTCHA-194.14"]
        self.assertAlmostEqual(cha["p"], 0.153, delta=0.01)
        self.assertEqual(cha["price_ts"], NOW.timestamp())
        self.assertIn("unmapped", snap["markets"]["KXPOKEMON-26OCTMEWEX-84.4"]["err"])
        self.assertEqual(snap["unmapped"], ["KXPOKEMON-26OCTMEWEX"])
        self.assertEqual(snap["errors"], [])
        self.assertEqual(snap["prices"]["714372"]["sellers"], 71.0)
        # only mapped items are read from TCGplayer
        self.assertTrue(all("/714372/" in u for u in self.s.calls))

    def test_meta_cadence_and_a_live_map(self):
        w = self._watch()
        t0 = NOW.timestamp()
        w.refresh(t0)
        w.refresh(t0 + 180)
        self.assertEqual(self.kalshi_calls, 1)           # markets every 15 minutes
        n_details = sum(u.endswith("/details") for u in self.s.calls)
        self.assertEqual(n_details, 1)
        # a new month mapped on main: picked up on the next refresh by mtime
        self._write_map({"KXPOKEMON-26OCTCHA": CHA_SPEC,
                         "KXPOKEMON-26OCTMEWEX": {"item": "Mew ex", "tcgplayer_id": 696688,
                                                  "kind": "card", "printing": "Foil"}})
        os.utime(self.map, (t0 + 5, t0 + 5))
        self.s.pricepoints[696688] = [{"printingType": "Foil", "marketPrice": 70.33,
                                       "listedMedianPrice": 86.82}]
        self.s.sellers[696688] = 189.0
        snap = w.refresh(t0 + 360)
        self.assertIn("p", snap["markets"]["KXPOKEMON-26OCTMEWEX-84.4"])
        self.assertEqual(snap["unmapped"], [])

    def test_a_jump_is_stamped_and_a_failed_read_ages_out(self):
        w = self._watch()
        t0 = NOW.timestamp()
        w.refresh(t0)
        self.s.pricepoints[714372] = [dict(PP_CHA[1], marketPrice=172.50)]
        snap = w.refresh(t0 + 180)                        # +0.4%: not a jump
        self.assertIsNone(snap["markets"]["KXPOKEMON-26OCTCHA-194.14"]["moved_at"])
        self.s.pricepoints[714372] = [dict(PP_CHA[1], marketPrice=190.00)]
        e = w.refresh(t0 + 360)["markets"]["KXPOKEMON-26OCTCHA-194.14"]
        self.assertEqual(e["moved_at"], t0 + 360)
        self.assertAlmostEqual(e["last_move"], math.log(190 / 172.5), places=4)
        self.s.down.add(714372)                            # TCGplayer stops answering
        snap = w.refresh(t0 + 540)
        e = snap["markets"]["KXPOKEMON-26OCTCHA-194.14"]
        self.assertEqual((e["v0"], e["price_ts"]), (190.0, t0 + 360))   # last good
        self.assertTrue(any(x.startswith("price 714372") for x in snap["errors"]))

    def test_no_seller_count_fails_closed_until_one_arrives(self):
        self.s.sellers[714372] = None
        w = self._watch()
        snap = w.refresh(NOW.timestamp())
        self.assertIn("seller count",
                      snap["markets"]["KXPOKEMON-26OCTCHA-194.14"]["err"])
        self.s.sellers[714372] = 71.0                     # retried before the meta refresh
        snap = w.refresh(NOW.timestamp() + 180)
        self.assertIn("p", snap["markets"]["KXPOKEMON-26OCTCHA-194.14"])

    def test_a_broken_map_keeps_the_last_good_one(self):
        w = self._watch()
        w.refresh(NOW.timestamp())
        with open(self.map, "w", encoding="utf-8") as f:
            f.write("{not json")
        os.utime(self.map, (NOW.timestamp() + 9, NOW.timestamp() + 9))
        snap = w.refresh(NOW.timestamp() + 180)
        self.assertIn("p", snap["markets"]["KXPOKEMON-26OCTCHA-194.14"])
        self.assertTrue(any(x.startswith("map:") for x in snap["errors"]))

    def test_load_products_drops_malformed_entries(self):
        self._write_map({"A": {"item": "x", "tcgplayer_id": "12", "kind": "card"},
                         "B": {"item": "x", "tcgplayer_id": "abc"},
                         "C": {"item": "x", "tcgplayer_id": 5, "kind": "graded"},
                         "D": {"tcgplayer_id": 5}, "E": "nope"})
        got = pf.load_products(self.map)
        self.assertEqual(list(got), ["A"])
        self.assertEqual((got["A"]["tcgplayer_id"], got["A"]["printing"]), (12, None))

    def test_suggest_lists_exact_names_first(self):
        self.s.search = [
            {"productId": 717605, "productName": "Mew ex - 152/128",
             "setName": "ME: 30th Celebration", "marketPrice": 98.18},
            {"productId": 704193, "productName": "30th Celebration Figure Collection [Mew]",
             "setName": "ME: 30th Celebration", "marketPrice": 124.92},
            {"productId": 696688, "productName": "Mew ex - 158/128",
             "setName": "ME: 30th Celebration", "marketPrice": 70.33}]
        self.s.pricepoints[717605] = [{"printingType": "Foil", "marketPrice": 98.18,
                                       "listedMedianPrice": 99.0}]
        self.s.pricepoints[696688] = [{"printingType": "Foil", "marketPrice": 70.33,
                                       "listedMedianPrice": 86.82}]
        lines = pf.suggest(self._get_json, session=self.s, products_file=self.map)
        self.assertTrue(lines[0].startswith("KXPOKEMON-26OCTMEWEX  item 'Mew ex'"))
        self.assertEqual(len(lines), 3)                  # the two exact names only
        self.assertIn("717605", lines[1])
        self.assertIn("696688", lines[2])


class TestShippedMap(unittest.TestCase):
    def test_the_october_map_matches_the_listed_rules(self):
        got = pf.load_products(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                            "pokemon_products.json"))
        want = {"KXPOKEMON-26OCTCHA": ("Charizard", 714372),
                "KXPOKEMON-26OCTMEWEX": ("Mew ex", 696688),
                "KXPOKEMON-26OCTPIKEX109": ("Pikachu ex - 109 (30th Celebration)", 713256),
                "KXPOKEMON-26OCTMEWRRG": ("Mew - R/RGB", 717607),
                "KXPOKEMON-26OCTMEWGRG": ("Mew - G/RGB", 717608),
                "KXPOKEMON-26OCTMEWBRG": ("Mew - B/RGB", 717609),
                "KXPOKEMON-26OCT30TCELPO": (
                    "30th Celebration Pokemon Center Elite Trainer Box", 704144)}
        self.assertEqual({k: (v["item"], v["tcgplayer_id"]) for k, v in got.items()}, want)
        self.assertEqual(got["KXPOKEMON-26OCT30TCELPO"]["kind"], "sealed")
        self.assertEqual(got["KXPOKEMON-26OCT30TCELPO"]["printing"], "Normal")


if __name__ == "__main__":
    unittest.main()
