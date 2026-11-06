"""Tests for combo_scorecard.py (the whole-tape scorecard, 2026-11-06). Synthetic
files and a fake Kalshi read; no network."""
import json
import math
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
from datetime import datetime, timezone

import combo_maker as cm
import combo_scorecard as sc

T0 = 1793925000.0               # 2026-11-06 ~00:30Z
DAY = "2026-11-06"
A, B, C = "KXNBAGAME-26NOV06BOSCHA-BOS", "KXNHLGAME-26NOV06TORMTL-TOR", "KXNCAAMBGAME-26NOV06DUKEUNC-DUKE"
SG = "KXNBASPREAD-26NOV06BOSCHA-BOS5"                 # same game as A
PROP = "KXNBAPTS-26NOV06PHIMIA-PHIJEMBIID1-30"         # a prop on another game
X = "KXNBAGAME-26NOV06LALDEN-LAL"                     # never priced
START = T0 + 3600.0                                    # every game here starts an hour after T0
EXP = START + 3 * 3600                                 # Kalshi's expiry: start + 3 h


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")


def tape_rows():
    """The maker's leg tape: at T0 A 0.60 (DraftKings 0.62), B 0.50, C 0.30; at T0 + 50
    min (10 min before the start) A 0.65 -- the closes A 0.65, B 0.50, C 0.30."""
    return [{"k": "legs", "t": T0, "m": {A: [0.59, 0.61, 0.62, None, EXP], B: [0.49, 0.51, None, None, EXP],
                                          C: [0.29, 0.31, None, None, EXP]}},
            {"k": "legs", "t": T0 + 3000, "m": {A: [0.64, 0.66, None, None], B: [0.49, 0.51, None, None],
                                                 C: [0.29, 0.31, None, None]}}]


def exec_rec(ts, legs, px, ct, cr, side="yes"):
    return {"k": "exec", "ts_ms": int(ts * 1000), "mt": "KXMVECROSSCATEGORY-S2026Q-" + cr, "px": f"{px:.4f}",
            "ct": f"{ct:.2f}", "side": side, "legs": legs, "cr": cr}


PRINTS = [exec_rec(T0 + 100, [[A, "yes"], [B, "yes"]], 0.31, 100.0, "r1"),     # fk 0.30, fmax 0.31, close 0.325
          exec_rec(T0 + 120, [[A, "yes"], [C, "no"]], 0.44, 50.0, "r2"),       # fk 0.42, fmax 0.434, close 0.455
          exec_rec(T0 + 130, [[A, "yes"], [SG, "yes"]], 0.40, 10.0, "r3"),     # same game
          exec_rec(T0 + 140, [[A, "yes"], [PROP, "yes"]], 0.20, 10.0, "r3"),   # a prop
          exec_rec(T0 + 150, [[A, "yes"], [X, "yes"]], 0.30, 10.0, "r3")]      # a leg unpriced


class FakeGet:
    def __init__(self, markets, refuse_after=None):
        self.mk, self.calls, self.refuse_after = markets, [], refuse_after

    def __call__(self, params):
        self.calls.append(params["tickers"].split(","))
        if self.refuse_after is not None and len(self.calls) > self.refuse_after:
            return None
        return {"markets": [self.mk[t] for t in params["tickers"].split(",") if t in self.mk]}


def kmarket(tk, result="", status="active"):
    return {"ticker": tk, "result": result, "status": status, "expected_expiration_time": iso(EXP)}


class TapeAndPrices(unittest.TestCase):
    def test_the_leg_tape(self):
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(os.path.join(d, f"legs_{DAY}.jsonl"), tape_rows() + [
                {"k": "legs", "t": T0 + 3100, "m": {X: [0.0, 1.0, None, None]}}])     # no book: no mid
            t = sc.load_tape(d, DAY)
            self.assertEqual(t.source, "tape")
            self.assertEqual(t.at(A, T0 + 300), (T0, 0.60, 0.62, None))
            self.assertAlmostEqual(t.at(A, T0 + 3100)[1], 0.65)
            self.assertIsNone(t.at(A, T0 + 3000 + sc.TAPE_AGE_S + 1))                 # too old
            self.assertIsNone(t.at(X, T0 + 3200))
            self.assertEqual(t.exp[A], EXP)
            self.assertAlmostEqual(t.close(A, START), 0.65)                         # the last before start - 60 s
            self.assertIsNone(sc.load_tape(d, "2026-11-08"))

    def test_mids_backed_out_of_our_quotes(self):
        p = {(A, "yes"): 0.6, (B, "yes"): 0.5, (C, "no"): 0.7}
        combos = [[(A, "yes"), (B, "yes")], [(A, "yes"), (C, "no")], [(B, "yes"), (C, "no")],
                  [(A, "yes"), (B, "yes"), (C, "no")]]
        rows = []
        for i in range(3):
            for legs in combos:
                fair = math.prod(p[l] for l in legs)
                rows.append({"k": "quote", "at": T0 + i, "legs": [list(l) for l in legs], "fair": fair * 1.02,
                             "fair_k": fair})                                       # fair_k: Kalshi's alone
        rows.append({"k": "quote", "at": T0 + 5, "legs": [[X, "yes"], [A, "yes"]], "fair": 0.3})   # X: 1 quote
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(os.path.join(d, f"combo_{DAY}.jsonl"), rows)
            t = sc.implied_tape(d, DAY)
            self.assertEqual(t.source, "implied")
            for tk, mid in ((A, 0.6), (B, 0.5), (C, 0.3)):
                self.assertAlmostEqual(t.at(tk, T0 + 60)[1], mid, places=6)
            self.assertIsNone(t.at(X, T0 + 60))                                     # under 3 quotes
            self.assertIsNone(t.at(A, T0 + sc.WINDOW_S * 2))                        # another window


class ResultsCache(unittest.TestCase):
    def test_final_legs_are_read_once_and_a_refusal_stops(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, sc.RESULTS_NAME)
            r = sc.Results(path)
            get = FakeGet({A: kmarket(A, "yes", "finalized"), B: kmarket(B), C: kmarket(C, "", "finalized")})
            self.assertEqual(r.fetch([A, B, C], get, now=T0), 0)
            self.assertEqual((r.result(A), r.result(B), r.result(C)), ("yes", None, "void"))
            self.assertEqual(r.exp(B), EXP)
            again = sc.Results(path)                                              # kept on disk
            get2 = FakeGet({B: kmarket(B, "no", "finalized")})
            again.fetch([A, B, C], get2, now=T0)
            self.assertEqual(get2.calls, [[B]])                                   # only the one not final
            self.assertEqual(again.result(B), "no")
            many = [f"KXNBAGAME-26NOV06G{i:03d}-T" for i in range(250)]
            self.assertEqual(sc.Results().fetch(many, FakeGet({}, refuse_after=1)), 150)   # 2nd call refused

    def test_outcome(self):
        r = sc.Results()
        r.d = {A: {"r": "yes"}, B: {"r": "no"}, C: {"r": None}, X: {"r": "void"}}
        self.assertEqual(sc.outcome([(A, "yes"), (B, "yes")], r), 0)            # B lost
        self.assertEqual(sc.outcome([(A, "yes"), (B, "no")], r), 1)
        self.assertIsNone(sc.outcome([(A, "yes"), (C, "yes")], r))              # C open
        self.assertEqual(sc.outcome([(B, "yes"), (C, "yes")], r), 0)            # lost already
        self.assertIsNone(sc.outcome([(A, "yes"), (X, "yes")], r))              # void


class Scoring(unittest.TestCase):
    def setUp(self):
        self.tape = sc.Tape()
        for rec in tape_rows():
            for tk, row in rec["m"].items():
                self.tape.add(tk, rec["t"], (row[0] + row[1]) / 2, row[2], row[3])
                if len(row) > 4:
                    self.tape.exp[tk] = row[4]
        self.tape.finish()
        self.res = sc.Results()
        self.res.d = {A: {"r": "yes", "x": EXP}, B: {"r": "no", "x": EXP}, C: {"r": "no", "x": EXP}}
        self.now = T0 + 7200

    def test_prices_closes_outcomes_and_skips(self):
        rows, skip = sc.price_prints([p for p in PRINTS], self.tape, self.res, self.now)
        self.assertEqual(dict(skip), {"same-game": 1, "props / extras": 1, "a leg unpriced": 1})
        r1, r2 = rows
        self.assertAlmostEqual(r1["fk"], 0.30)
        self.assertAlmostEqual(r1["fmax"], 0.31)                                # DraftKings' 0.62 on A
        self.assertAlmostEqual(r1["close"], 0.325)
        self.assertEqual((r1["y"], r2["y"]), (0, 1))
        self.assertAlmostEqual(r2["fk"], 0.42)
        self.assertAlmostEqual(r2["close"], 0.65 * 0.70)
        early, _ = sc.price_prints(PRINTS[:1], self.tape, self.res, START - 1)  # not started: no close yet
        self.assertIsNone(early[0]["close"])

    def test_segments_our_price_requesters_sources(self):
        cfg = cm.Config(markup=0.04, markup_per_leg=0.01, min_legs=2, max_legs=8)
        s = sc.score(PRINTS, self.tape, self.res, self.now, cfg=cfg, no_prints=4)
        two = s["segments"]["2"]
        self.assertEqual((two["prints"], two["settled"], two["hits"], two["closed"]), (2, 2, 1, 2))
        self.assertAlmostEqual(two["clear_median"], (0.31 / 0.30 - 1 + 0.44 / 0.42 - 1) / 2)
        prem = 100 * 0.31 + 50 * 0.44
        self.assertAlmostEqual(two["paid_over_premium"], 50.0 / prem)
        self.assertAlmostEqual(two["over_close_wtd"], (100 * (0.31 - 0.325) + 50 * (0.44 - 0.455)) / prem)
        op = {o["markup"]: o for o in s["our_price"]["2"]}
        self.assertEqual((op[0.0]["won"], op[0.02]["won"], op["live rule"]["won"]), (2, 0, 0))
        q2 = 0.434
        c1, c2 = 100.0, 50 * 0.44 / q2
        want = (c1 * (0.31 - 0.325) - c1 * sc.fee_of(0.31, False)) + (c2 * (q2 - 0.455) - c2 * sc.fee_of(q2, False))
        self.assertAlmostEqual(op[0.0]["vs_close"], round(want, 2), places=2)
        self.assertAlmostEqual(op[0.0]["pnl"], round(c1 * 0.31 - c1 * sc.fee_of(0.31, False)
                                                     + c2 * (q2 - 1) - c2 * sc.fee_of(q2, False), 2), places=2)
        self.assertEqual([x["cr"] for x in s["requesters"]["top"]], ["r1", "r2"])
        nba = s["sources"]["NBA"]                                               # A, an hour out: 0.60 / DK 0.62
        self.assertAlmostEqual(nba["Kalshi mid"]["mae_close"], 0.05)
        self.assertAlmostEqual(nba["DraftKings"]["mae_close"], 0.03)
        self.assertAlmostEqual(nba["blend"]["mae_close"], 0.04)
        self.assertAlmostEqual(nba["DraftKings"]["brier"], (0.62 - 1) ** 2)
        self.assertAlmostEqual(nba["DraftKings"]["k_brier"], (0.60 - 1) ** 2)
        md = sc.render_md(DAY, s)
        for want_txt in ("# Combo scorecard, 2026-11-06", "## Where the market clears", "| 2 | 2 |",
                         "**2 legs** (2 prints)", "| live rule |", "## Requesters", "| NBA | DraftKings |"):
            self.assertIn(want_txt, md)

    def test_the_live_config_from_the_start_line(self):
        with tempfile.TemporaryDirectory() as d:
            write_jsonl(os.path.join(d, f"combo_{DAY}.jsonl"), [
                {"k": "start", "t": T0, "cfg": {"markup": 0.04, "markup_per_leg": 0.01, "min_legs": 2, "max_legs": 8,
                                                "longshot_markup": [[0.05, 0.02]], "sports": ["NBA"]}}])
            cfg = sc.live_config(d, DAY)
            self.assertEqual((cfg.markup, cfg.max_legs, cfg.longshot_markup), (0.04, 8, ((0.05, 0.02),)))
            self.assertAlmostEqual(sc.live_price(0.30, [[A, "yes"], [B, "yes"]], cfg), 0.312)
            self.assertIsNone(sc.live_config(d, "2026-11-08"))


class EndToEnd(unittest.TestCase):
    def test_main_writes_the_report(self):
        with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as md:
            write_jsonl(os.path.join(rd, f"combo_{DAY}.jsonl"),
                        PRINTS + [exec_rec(T0 + 200, [[A, "yes"], [B, "yes"]], 0.69, 10.0, "r9", side="no"),
                                  {"k": "rfq", "ms": int(T0 * 1000), "id": "x"}])
            write_jsonl(os.path.join(md, f"legs_{DAY}.jsonl"), tape_rows())
            write_jsonl(os.path.join(md, f"combo_{DAY}.jsonl"), [{"k": "start", "t": T0, "cfg": {"markup": 0.04}}])
            self.assertEqual(sc.main(["--day", DAY, "--rfq-dir", rd, "--maker-dir", md, "--no-results"]), 0)
            with open(os.path.join(rd, f"scorecard_{DAY}.json"), encoding="utf-8") as f:
                js = json.load(f)
            self.assertEqual((js["scored"], js["prints"], js["no_side_prints"], js["tape"]), (2, 5, 1, "tape"))
            self.assertTrue(os.path.exists(os.path.join(rd, f"scorecard_{DAY}.md")))
            with tempfile.TemporaryDirectory() as empty:
                self.assertEqual(sc.main(["--day", DAY, "--rfq-dir", rd, "--maker-dir", empty, "--no-results"]), 2)


class LayeredTape(unittest.TestCase):
    def test_the_tape_first_the_implied_mids_behind_it(self):
        tape = sc.Tape().finish()
        tape.add(A, T0 + 3000, 0.65)
        tape.exp[A] = EXP
        tape.finish()
        imp = sc.Tape(max_age=sc.WINDOW_S, source="implied")
        imp.add(A, T0, 0.60)
        imp.add(B, T0, 0.50)
        imp.finish()
        lt = sc.Layered(tape, imp)
        self.assertEqual(lt.source, "tape+implied")
        self.assertAlmostEqual(lt.at(A, T0 + 100)[1], 0.60)        # before the tape: implied
        self.assertAlmostEqual(lt.at(A, T0 + 3100)[1], 0.65)       # the tape
        self.assertAlmostEqual(lt.at(B, T0 + 100)[1], 0.50)
        self.assertEqual(lt.exp[A], EXP)
        self.assertAlmostEqual(lt.close(A, START), 0.65)

    def test_main_layers_a_tape_that_starts_late(self):
        quotes = [{"k": "quote", "at": T0 + i, "legs": [[A, "yes"], [B, "yes"]], "fair": 0.30} for i in range(3)] + \
                 [{"k": "quote", "at": T0 + i, "legs": [[A, "yes"], [C, "no"]], "fair": 0.42} for i in range(3)] + \
                 [{"k": "quote", "at": T0 + i, "legs": [[B, "yes"], [C, "no"]], "fair": 0.35} for i in range(3)]
        with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as md:
            write_jsonl(os.path.join(rd, f"combo_{DAY}.jsonl"), PRINTS)
            write_jsonl(os.path.join(md, f"legs_{DAY}.jsonl"), [tape_rows()[1]])       # starts at T0 + 50 min
            write_jsonl(os.path.join(md, f"combo_{DAY}.jsonl"), quotes)
            self.assertEqual(sc.main(["--day", DAY, "--rfq-dir", rd, "--maker-dir", md, "--no-results"]), 0)
            with open(os.path.join(rd, f"scorecard_{DAY}.json"), encoding="utf-8") as f:
                js = json.load(f)
            self.assertEqual((js["tape"], js["scored"]), ("tape+implied", 2))           # the early prints priced


class EmailAndReads(unittest.TestCase):
    """The morning email (Jack 2026-11-06: "yes i want the scorecard every morning")
    and results read signed first through kalshi_reads."""

    def test_results_read_signed_first_and_none_on_failure(self):
        import kalshi_reads as kr
        with mock.patch.object(kr, "kalshi_get", return_value={"markets": []}) as g:
            self.assertEqual(sc.kalshi_markets({"tickers": A}), {"markets": []})
            self.assertEqual(g.call_args.args[0], "/markets")
        with mock.patch.object(kr, "kalshi_get", side_effect=kr.KalshiReadError("both routes")):
            self.assertIsNone(sc.kalshi_markets({"tickers": A}))

    def test_html_and_subject(self):
        h = sc.md_to_html("# T\n\n**2 legs** x<y\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        for want in ("<h2>T</h2>", "<b>2 legs</b> x&lt;y", "<th style", ">1</td>"):
            self.assertIn(want, h)
        self.assertNotIn("---", h)
        s = {"scored": 4001, "segments": {"2": {"clear_median": 0.013, "fee_share": 0.021}},
             "our_price": {"2": [{"markup": "live rule", "won": 11, "of": 367}]}}
        self.assertEqual(sc.subject_for(DAY, s), "Combo scorecard 2026-11-06: 4,001 prints scored; 2 legs clear "
                                                 "+1.3% over the mids (fee 2.1%); our 2-leg price beats 3% of them")

    def test_main_emails_once_a_day(self):
        sent = []

        class Alerter:
            enabled = True

            def __init__(self, tag, live):
                self.tag = tag

            def send_message(self, body, subject="", html=None):
                sent.append((self.tag, subject, html))
                return True
        fake_imm = types.SimpleNamespace(Alerter=Alerter)
        with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as md,                 mock.patch.dict(sys.modules, {"imm_quote_gaps": types.ModuleType("imm_quote_gaps"),
                                              "incentive_mm": fake_imm}),                 mock.patch.object(sc, "low_priority"):
            write_jsonl(os.path.join(rd, f"combo_{DAY}.jsonl"), PRINTS)
            write_jsonl(os.path.join(md, f"legs_{DAY}.jsonl"), tape_rows())
            args = ["--day", DAY, "--rfq-dir", rd, "--maker-dir", md, "--no-results", "--email"]
            self.assertEqual(sc.main(args), 0)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][0], "COMBO-SCORE")
            self.assertTrue(sent[0][1].startswith("Combo scorecard 2026-11-06: 2 prints scored"))
            self.assertIn("<table", sent[0][2])
            self.assertEqual(sc.main(args), 0)                                  # the marker: not again today
            self.assertEqual(len(sent), 1)
            self.assertEqual(sc.main(args + ["--test"]), 0)                     # --test: again, no marker
            self.assertEqual(len(sent), 2)
            Alerter.enabled = False
            os.remove(next(os.path.join(rd, f) for f in os.listdir(rd) if f.startswith("scorecard_sent_")))
            self.assertEqual(sc.main(args), 1)                                  # no credentials: fail loudly


if __name__ == "__main__":
    unittest.main()
