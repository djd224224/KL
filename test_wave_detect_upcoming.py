"""wave_detect.py --upcoming (Jack 2026-11-14: "market-wave skill should also scan upcoming
rewards markets and callout if a wave would be greenlit so i have advanced notice"): Kalshi's
upcoming programs judged per start window the way the 24/7 check will judge them at the
start. No network (the reads are stand-ins) and every file lives in a temp dir."""
import argparse
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

if "kalshi_reads" not in sys.modules:
    _m = types.ModuleType("kalshi_reads")
    _m.kalshi_get = lambda *a, **k: {}
    _m.kalshi_get_all = lambda *a, **k: []
    sys.modules["kalshi_reads"] = _m
import wave_detect as wd  # noqa: E402

NOW = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)      # Sat 1:00 PM ET
START = datetime(2026, 11, 15, 5, 0, tzinfo=timezone.utc)     # the daily roll, Sun 12:00 AM ET
LATER = datetime(2026, 11, 15, 16, 0, tzinfo=timezone.utc)    # Sun 11:00 AM ET


def z(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def prog(t, start, usd=25.0, days=1.0):
    return {"market_ticker": t, "incentive_type": "liquidity", "start_date": z(start),
            "end_date": z(start + timedelta(days=days)), "period_reward": int(usd * days * 10000),
            "target_size_fp": "1000", "discount_factor_bps": 5000}


def series_rows(s, n_events, per_event, start, **kw):
    return [prog(f"{s}-E{e}-M{k}", start, **kw) for e in range(n_events) for k in range(per_event)]


class Args(argparse.Namespace):
    def __init__(self, **kw):
        super().__init__(min_markets=40, min_pool=1000.0, min_days_out=14.0, min_soon_days=1.0, min_events=5,
                         horizon_hours=72.0, preflight=False, update_state=False, json=None, upcoming=True)
        for k, v in kw.items():
            setattr(self, k, v)


class StartWindows(unittest.TestCase):
    def test_starts_within_30_min_of_the_first_share_a_window(self):
        mk = {f"KXA-E-{i}": {"start": START + timedelta(minutes=m)} for i, m in enumerate((0, 10, 29, 30, 75))}
        wins = wd.start_windows(mk)
        self.assertEqual([(a, b, len(m)) for a, b, m in wins],
                         [(START, START + timedelta(minutes=29), 3),
                          (START + timedelta(minutes=30), START + timedelta(minutes=30), 1),
                          (START + timedelta(minutes=75), START + timedelta(minutes=75), 1)])


class UpcomingMarkets(unittest.TestCase):
    def test_next_program_only_and_the_horizon(self):
        rows = [prog("KXA-E-1", START), prog("KXA-E-1", START + timedelta(days=1)),     # back-to-back periods
                prog("KXA-E-2", NOW - timedelta(minutes=5)),                             # started: the live check's
                prog("KXA-E-3", NOW + timedelta(hours=80))]                              # past the 72 h horizon
        mk = wd.upcoming_markets(rows, NOW, 72)
        self.assertEqual(set(mk), {"KXA-E-1"})
        self.assertEqual(mk["KXA-E-1"]["start"], START)
        self.assertAlmostEqual(mk["KXA-E-1"]["pool"], 25.0)


class MainUpcoming(unittest.TestCase):
    """main_upcoming end to end on stand-in reads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self.saved = {k: getattr(wd, k) for k in ("STATE", "UPCOMING_STATE", "LIST", "REGISTRY", "bot_quoted",
                                                  "read_tickers", "read_meta", "run_preflight",
                                                  "run_wave_preflight")}
        self.saved_wave_pf = wd.run_wave_preflight
        self.saved_imm = (wd.imm._fair_reader_client, wd.snp.fetch_programs_raw,
                          wd.imm.IncentiveMarketMaker.__dict__["_allowed"],
                          wd.imm.IncentiveMarketMaker.__dict__["_blocked"])
        wd.STATE = os.path.join(d, "wave_detect_state.json")
        wd.UPCOMING_STATE = os.path.join(d, "wave_upcoming_state.json")
        wd.LIST = os.path.join(d, "list.json")
        wd.REGISTRY = os.path.join(d, "waves.json")
        with open(wd.REGISTRY, "w") as fh:
            json.dump({"waves": {"w1": {"series": ["KXREG"]}}}, fh)
        wd.bot_quoted = lambda now: set()
        wd.imm._fair_reader_client = lambda: None
        wd.imm.IncentiveMarketMaker._allowed = classmethod(lambda cls, t: t.startswith("KXMEMB"))
        wd.imm.IncentiveMarketMaker._blocked = staticmethod(lambda t: False)
        self.reads = []
        self.meta = {"KXGOODA": {"category": "Politics", "title": "Who will win"},
                     "KXSPORT": {"category": "Sports", "title": "Who wins the game"},
                     "KXGOLDEVENT": {"category": "Commodities", "title": "Gold price after the release"},
                     "KXNEARX": {"category": "Politics", "title": "Will it pass"}}
        self.settle = {"KXGOODA": START + timedelta(days=60), "KXSPORT": START + timedelta(days=60),
                       "KXGOLDEVENT": LATER + timedelta(days=2), "KXNEARX": START + timedelta(days=13.9)}

        def read_tickers(tick):
            self.reads.append(sorted(tick))
            out = {}
            for t in tick:
                s, e = t.split("-")[0], t.rsplit("-", 1)[0]
                out[t] = {"ticker": t, "event_ticker": e, "title": f"{e} title",
                          "close_time": z(self.settle.get(s, START + timedelta(days=60))),
                          "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.46"}
            return out
        wd.read_tickers = read_tickers
        wd.read_meta = lambda s: self.meta.get(s, {"category": "Politics"})
        self.rows = (series_rows("KXGOODA", 6, 8, START)                  # FIT: 48 markets -> a wave
                     + series_rows("KXSPORT", 6, 2, START)                # CHECK (Sports), denied in the wave
                     + series_rows("KXONE", 1, 9, START)                  # FEW
                     + series_rows("KXREG", 6, 2, START)                  # a registry wave's: LISTED
                     + series_rows("KXMEMB", 6, 2, START)                 # the bot's family: MEMBER
                     + series_rows("KXGOLDEVENT", 6, 8, LATER))           # a live-price ladder: needs you
        wd.snp.fetch_programs_raw = lambda client, statuses=("active",): list(self.rows)

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(wd, k, v)
        wd.imm._fair_reader_client, wd.snp.fetch_programs_raw = self.saved_imm[0], self.saved_imm[1]
        wd.imm.IncentiveMarketMaker._allowed = self.saved_imm[2]
        wd.imm.IncentiveMarketMaker._blocked = self.saved_imm[3]
        self.tmp.cleanup()

    def run_it(self, **kw):
        out = os.path.join(self.tmp.name, "up.json")
        rc = wd.main_upcoming(Args(json=out, **kw), NOW)
        with open(out) as fh:
            return rc, json.load(fh)

    def test_a_wave_would_be_greenlit_at_the_roll_and_the_ladder_needs_you(self):
        rc, res = self.run_it()
        self.assertEqual(rc, 3)
        w1, w2 = res["windows"]
        self.assertEqual((w1["start"], w1["notice"], w1["fresh_series"]), (z(START), "greenlit", ["KXGOODA"]))
        self.assertEqual(w1["new_check_series"], ["KXSPORT"])
        v = {r["series"]: r["verdict"] for r in w1["series"]}
        self.assertEqual(v, {"KXGOODA": "FIT", "KXSPORT": "CHECK", "KXONE": "FEW", "KXREG": "LISTED",
                             "KXMEMB": "MEMBER"})
        self.assertEqual((w2["start"], w2["notice"], w2["new_check_series"]), (z(LATER), "needs-you", ["KXGOLDEVENT"]))
        gold = w2["series"][0]
        self.assertTrue(any("live-price ladder" in x for x in gold["why"]))
        # LISTED / MEMBER / FEW series need no Kalshi read
        read = {t.split("-")[0] for batch in self.reads for t in batch}
        self.assertEqual(read, {"KXGOODA", "KXSPORT", "KXGOLDEVENT"})

    def test_noticed_once_per_start(self):
        rc, _ = self.run_it(update_state=True)
        self.assertEqual(rc, 3)
        rc, res = self.run_it(update_state=True)
        self.assertEqual(rc, 0)
        self.assertEqual([w["notice"] for w in res["windows"]], [None, None])
        self.assertTrue(res["windows"][0]["wave"])          # still a wave, just already told
        with open(wd.UPCOMING_STATE) as fh:
            st = json.load(fh)
        self.assertIn(f"KXGOODA@{z(START)}", st["noticed"])
        self.assertIn(f"KXSPORT@{z(START)}", st["noticed"])     # denied in a wave: marked with it, as live does
        self.assertIn(f"KXGOLDEVENT@{z(LATER)}", st["noticed"])

    def test_a_series_the_live_check_alerted_never_counts(self):
        with open(wd.STATE, "w") as fh:
            json.dump({"alerted": {"KXGOODA": "2026-11-08T23:08:00Z"}}, fh)
        rc, res = self.run_it()
        w1 = res["windows"][0]
        self.assertFalse(w1["wave"])
        self.assertEqual(w1["series"][0]["alerted_before"], "2026-11-08T23:08:00Z")
        self.assertEqual(rc, 4)           # the ladder window still needs you

    def test_dates_are_measured_from_the_start(self):
        # 14.3 days from now, 13.9 from the start: the check at the start sees NEAR, not FIT
        self.rows = series_rows("KXNEARX", 6, 8, START)
        rc, res = self.run_it()
        r = res["windows"][0]["series"][0]
        self.assertEqual((r["verdict"], r["days_out"]), ("NEAR", 13.9))
        self.assertEqual(res["windows"][0]["new_near_series"], ["KXNEARX"])
        self.assertEqual(rc, 3)

    def test_preflight_runs_on_greenlit_windows_only(self):
        seen = []
        wd.run_preflight = lambda w, now, **kw: seen.append(w["start"]) or {"exit": 0, "fail": [], "warn": [], "pass": 9}
        rc, res = self.run_it(preflight=True)
        self.assertEqual(seen, [z(START)])
        self.assertEqual(res["windows"][0]["preflight"]["pass"], 9)
        self.assertIsNone(res["windows"][1]["preflight"])

    def test_a_new_shape_needs_you_with_preflight_as_if_yes(self):
        # Jack 2026-11-14: "for upcoming new shapes, notify me and it's my call so dont default to
        # turning it on e.g. KXYTVIEWSHIGH"
        self.meta["KXSHOW"] = {"category": "Entertainment", "title": "Highest daily views", "tags": ["Views"]}
        self.rows = series_rows("KXSHOW", 6, 8, START)
        seen = []
        wd.run_preflight = lambda w, now, **kw: seen.append(kw) or {"exit": 0, "fail": [], "warn": [], "pass": 9}
        rc, res = self.run_it(preflight=True)
        self.assertEqual(rc, 4)
        w = res["windows"][0]
        self.assertEqual((w["notice"], w["new_shape_series"], w["new_shape_over_bar"]),
                         ("needs-you", ["KXSHOW"], True))
        self.assertEqual(w["series"][0]["verdict"], "NEW")
        self.assertEqual(seen, [{"fit": ["KXSHOW"], "near": [], "auto": False}])

    def test_a_registry_relight_over_the_bar_is_noticed_once(self):
        self.rows = series_rows("KXREG", 6, 8, START)          # w1's series, not on the list now
        seen = []
        wd.run_wave_preflight = lambda name, now: seen.append(name) or {"exit": 0, "fail": [], "warn": [], "pass": 7}
        try:
            rc, res = self.run_it(preflight=True, update_state=True)
            self.assertEqual(rc, 5)
            w = res["windows"][0]
            self.assertIsNone(w["notice"])
            self.assertEqual(w["relight"]["waves"]["w1"]["markets"], 48)
            self.assertEqual((w["relight"]["notice"], w["relight"]["fresh_series"]), (True, ["KXREG"]))
            self.assertEqual(w["relight"]["preflight"]["w1"]["pass"], 7)
            self.assertEqual(seen, ["w1"])
            self.assertEqual(self.reads, [])                    # LISTED needs no read
            rc, res = self.run_it(update_state=True)
            self.assertEqual(rc, 0)
            self.assertFalse(res["windows"][0]["relight"]["notice"])
        finally:
            wd.run_wave_preflight = self.saved_wave_pf

    def test_a_relight_already_on_the_list_is_a_renewal(self):
        self.rows = series_rows("KXREG", 6, 8, START)
        with open(wd.LIST, "w") as fh:
            json.dump({"markets": {r["market_ticker"]: {"stop": z(START)} for r in self.rows}}, fh)
        rc, res = self.run_it()
        self.assertEqual(rc, 0)
        self.assertIsNone(res["windows"][0]["relight"])


if __name__ == "__main__":
    unittest.main()
