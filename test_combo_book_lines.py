"""Tests for combo_book_lines.py (the sportsbook's vig-free line as the combo
shadow's second fair, 2026-11-05). No network."""
import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone

import combo_book_lines as bl
import combo_rfq_score as cs

T0 = 1793928000.0          # 2026-11-06 01:20Z
START = T0 + 2400          # tip 40 min later


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def espn(events):
    return {"events": events}


def espn_game(eid, home, away, start, hml=None, aml=None, nested=None, state="pre"):
    odds = {"provider": {"name": "DraftKings"}, "details": f"{home} -3.5", "overUnder": 225.5}
    if hml is not None:
        odds["homeTeamOdds"] = {"moneyLine": hml}
        odds["awayTeamOdds"] = {"moneyLine": aml}
    if nested is not None:
        odds["moneyline"] = nested
    return {"id": eid, "date": iso(start), "shortName": f"{away} @ {home}",
            "competitions": [{"odds": [odds], "status": {"type": {"state": state}},
                              "competitors": [{"homeAway": "home", "team": {"abbreviation": home}},
                                              {"homeAway": "away", "team": {"abbreviation": away}}]}]}


GSW = "KXNBAGAME-26NOV05MIAGSW-GSW"
MIA = "KXNBAGAME-26NOV05MIAGSW-MIA"
MKT = {GSW: {"ticker": GSW, "event_ticker": "KXNBAGAME-26NOV05MIAGSW",
             "expected_expiration_time": iso(START + 3 * 3600), "result": "yes"},
       MIA: {"ticker": MIA, "event_ticker": "KXNBAGAME-26NOV05MIAGSW",
             "expected_expiration_time": iso(START + 3 * 3600), "result": "no"}}


class Parsing(unittest.TestCase):
    def test_moneylines_and_no_vig(self):
        self.assertEqual(bl.american("+139"), 139.0)
        self.assertEqual(bl.american("EVEN"), 100.0)
        self.assertIsNone(bl.american("OFF"))
        self.assertIsNone(bl.american("50"))                         # not a moneyline
        self.assertAlmostEqual(bl.implied(-165), 165 / 265)
        p = bl.no_vig(-167, 140)                                       # MIA @ GS, 11/05
        self.assertAlmostEqual(p, (167 / 267) / (167 / 267 + 100 / 240))
        self.assertTrue(0.59 < p < 0.61)

    def test_both_espn_shapes(self):
        flat = espn_game("1", "GS", "MIA", START, hml=-167, aml=140)
        nested = espn_game("2", "MEM", "OKC", START, nested={"home": {"close": {"odds": "+139"}},
                                                            "away": {"close": {"odds": "-165"}}})
        no_odds = espn_game("3", "PHX", "ORL", START)
        rows = bl.odds_rows(espn([flat, nested, no_odds]), "KXNBAGAME", T0)
        self.assertEqual([(r["eid"], r["home"], r["hml"], r["aml"]) for r in rows],
                         [("1", "GS", -167.0, 140.0), ("2", "MEM", 139.0, -165.0)])
        self.assertEqual(rows[0]["prov"], "DraftKings")

    def test_day_of_event(self):
        self.assertEqual(bl.day_of_event("KXNBAGAME-26NOV05MIAGSW"), "20261105")
        self.assertEqual(bl.day_of_event("KXNFLGAME-26DEC14KCBUF"), "20261214")
        self.assertIsNone(bl.day_of_event("garbage"))


class Matching(unittest.TestCase):
    def lines(self):
        snaps = [{"lg": "KXNBAGAME", "eid": "1", "t": T0 - 600, "start": iso(START), "home": "GS",
                  "away": "MIA", "p_home": 0.58},
                 {"lg": "KXNBAGAME", "eid": "1", "t": T0 + 600, "start": iso(START), "home": "GS",
                  "away": "MIA", "p_home": 0.62}]
        return bl.Lines(snaps, [dict(snaps[0], p_home=0.65)])

    def test_a_leg_finds_its_game_and_side(self):
        L = self.lines()
        self.assertEqual(L.match(GSW, MKT[GSW]), (("KXNBAGAME", "1"), True))     # GSW starts with GS
        self.assertEqual(L.match(MIA, MKT[MIA]), (("KXNBAGAME", "1"), False))
        late = dict(MKT[GSW], expected_expiration_time=iso(START + 5 * 3600))     # start off by 2 h
        self.assertIsNone(L.match(GSW, late))
        self.assertIsNone(L.match("KXNBASPREAD-26NOV05MIAGSW-GSW5", {}))          # not a game winner

    def test_the_line_used_is_the_last_one_at_or_before_the_rfq(self):
        L = self.lines()
        key = ("KXNBAGAME", "1")
        self.assertEqual(L.p_home(key, T0), (0.58, "snapshot"))
        self.assertEqual(L.p_home(key, T0 + 700), (0.62, "snapshot"))
        self.assertEqual(L.p_home(key, T0 - 900), (0.65, "later"))               # nothing logged yet


class Calibration(unittest.TestCase):
    def test_rows_summary_and_render(self):
        book = cs.PriceBook()
        book.add({GSW: [(int(T0 - 30), 0.60, 0.61)], MIA: [(int(T0 - 30), 0.39, 0.40)]})
        snaps = [{"lg": "KXNBAGAME", "eid": "1", "t": T0 - 600, "start": iso(START), "home": "GS",
                  "away": "MIA", "p_home": 0.58}]
        L = bl.Lines(snaps)
        execs = [{"tid": "a", "rfq_ms": T0 * 1000, "legs": [[GSW, "yes"]]},
                 {"tid": "b", "rfq_ms": T0 * 1000, "legs": [[MIA, "no"]]},       # MIA loses = GSW
                 {"tid": "c", "rfq_ms": T0 * 1000, "legs": [[MIA, "yes"]]}]
        rows = bl.calibration(execs, MKT, book, L)
        self.assertEqual(len(rows), 3)
        a, b, c = rows
        self.assertAlmostEqual(a["p_kalshi"], 0.605)
        self.assertAlmostEqual(a["p_book"], 0.58)
        self.assertEqual((a["hit"], b["hit"], c["hit"]), (1.0, 1.0, 0.0))
        self.assertAlmostEqual(b["p_kalshi"], 1 - 0.395)
        self.assertAlmostEqual(c["p_book"], 0.42)
        s = bl.summarize(rows)
        al = s["all"]
        self.assertEqual((al["n"], al["settled"]), (3, 3))
        self.assertAlmostEqual(al["hit"], 2 / 3)
        self.assertAlmostEqual(al["brier_kalshi"], ((0.605 - 1) ** 2 + (0.605 - 1) ** 2 + 0.395 ** 2) / 3)
        self.assertIn("NBA", s["by_sport"])
        text = "\n".join(bl.render(s))
        self.assertIn("Brier", text)
        self.assertIn("| all | 3 | 3 |", text)
        self.assertIn("No game-winner leg matched", "\n".join(bl.render({})))


class Snapshots(unittest.TestCase):
    def test_a_pass_writes_new_or_moved_lines_only(self):
        calls = []

        def get(url, params):
            calls.append((url, params.get("dates")))
            if "basketball/nba" in url and params["dates"] == "20261105":
                return espn([espn_game("1", "GS", "MIA", START, hml=-167, aml=140)])
            return espn([])

        class W:
            def __init__(self):
                self.rows = []

            def write(self, r):
                self.rows.append(r)

            def flush(self):
                pass

        w, last = W(), {}
        now = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc).timestamp()       # 8 PM ET 11/05
        self.assertEqual(bl.snapshot_pass(w, last, now, get, log=lambda s: None), 1)
        self.assertEqual(bl.snapshot_pass(w, last, now, get, log=lambda s: None), 0)    # unchanged
        self.assertEqual(sum(a for _p, _g, a in bl.LEAGUES.values()), len(calls) // 2)
        self.assertEqual(w.rows[0]["p_home"], round(bl.no_vig(-167, 140), 5))

    def test_the_loop_writes_odds_files_and_stops(self):
        with tempfile.TemporaryDirectory() as d:
            stop = threading.Event()
            get = lambda url, params: espn([espn_game("9", "GS", "MIA", START, hml=-150, aml=130)])  # noqa: E731
            th = threading.Thread(target=bl.snapshot_loop, args=(d, stop),
                                  kwargs={"every": 3600, "get": get, "log": lambda s: None})
            th.start()
            for _ in range(100):
                if any(n.startswith("odds_") for n in os.listdir(d)):
                    break
                threading.Event().wait(0.05)
            stop.set()
            th.join(timeout=10)
            self.assertFalse(th.is_alive())
            name = next(n for n in os.listdir(d) if n.startswith("odds_"))
            with open(os.path.join(d, name), encoding="utf-8") as f:
                rows = [json.loads(x) for x in f]
            self.assertGreaterEqual(len(rows), 1)
            got = bl.load(d, date(2000, 1, 1), date(2100, 1, 1))
            self.assertEqual(len(got), len(rows))


if __name__ == "__main__":
    unittest.main()
