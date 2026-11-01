"""Tests for imm_unquoted.py (the unquoted series scored for adding) and its
two readers: the weekly email (imm_unquoted_weekly.py) and the dashboard's
Promising unquoted series section. Jack 2026-11-01: "yes" to a weekly email
of the unquoted list, and "also in the command center dashboard, make sure
the 'promising' unquoted markets section is built out and would include any
promising ones"."""

import json
import os
import shutil
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import imm_quote_gaps  # noqa: F401  (the launcher env, then incentive_mm)
import incentive_mm as imm
import imm_unquoted as iu

NOW = datetime(2026, 11, 8, 13, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def gap(series, event, pool, n=1, reason=iu.CANDIDATE_REASON, title=""):
    return {"event": event, "series": series, "n": n, "n_est": 0, "pool": pool, "est": None,
            "yld": None, "partial": False, "end": NOW.timestamp() + 5 * 86400,
            "reason": reason, "title": title}


class CandidatesTests(unittest.TestCase):

    def test_rolls_up_the_not_in_allowlist_rows_per_series(self):
        rows = [gap("KXA", "KXA-26NOV08", 100.0, n=3, title="A strike?"),
                gap("KXA", "KXA-26NOV15", 300.0, n=2),
                gap("KXB", "KXB-26NOV08", 50.0),
                gap("KXC", "KXC-26NOV08", 999.0, reason="blocklisted")]
        c = iu.candidates(rows)
        self.assertEqual(sorted(c), ["KXA", "KXB"])
        self.assertEqual(c["KXA"]["events"], ["KXA-26NOV15", "KXA-26NOV08"])    # biggest first
        self.assertEqual((c["KXA"]["n"], c["KXA"]["pool"], c["KXA"]["title"]), (5, 400.0, "A strike?"))


class SizeTests(unittest.TestCase):

    def test_x3_rides_the_size_mult_wire_and_is_put_back(self):
        s = "KXZZUNQTEST"
        self.assertNotIn(s, imm.SERIES_OVERRIDES)
        with iu.size_at(imm, s, 3.0):
            self.assertEqual(imm.SERIES_OVERRIDES[s].size_mult, 3.0)
            self.assertEqual(imm.applied_mention_mult(s), 3.0 * imm.size_test_mult(s))
        self.assertNotIn(s, imm.SERIES_OVERRIDES)
        with iu.size_at(imm, s, 1.0):
            self.assertNotIn(s, imm.SERIES_OVERRIDES)

    def test_a_hand_tuned_ladder_is_scaled_and_restored(self):
        s = "KXZZUNQLADDER"
        ov = imm.SeriesOverride(levels=[(0, 5), (1, 5)], max_position=50.0)
        imm.SERIES_OVERRIDES[s] = ov
        try:
            with iu.size_at(imm, s, 3.0):
                self.assertEqual(imm.series_levels(s), [(0, 15), (1, 15)])
                self.assertEqual(imm.series_max_position(s), 150.0)
            self.assertIs(imm.SERIES_OVERRIDES[s], ov)
        finally:
            imm.SERIES_OVERRIDES.pop(s, None)

    def test_a_family_size_compounds(self):
        s = "KXZZUNQFAM"
        imm.SERIES_OVERRIDES[s] = imm.SeriesOverride(size_mult=2.0)
        try:
            with iu.size_at(imm, s, 3.0):
                self.assertEqual(imm.SERIES_OVERRIDES[s].size_mult, 6.0)
            self.assertEqual(imm.SERIES_OVERRIDES[s].size_mult, 2.0)
        finally:
            imm.SERIES_OVERRIDES.pop(s, None)


class RealEstimatorTests(unittest.TestCase):
    """The bot's own estimator, offline, on a synthetic book: x3 must reach it."""

    def test_x3_earns_more_than_x1_on_the_real_estimator(self):
        t = "KXZZUNQREAL-26NOV20-T50"
        book = {"orderbook_fp": {"yes_dollars": [["0.40", "300"], ["0.38", "300"]],
                                 "no_dollars": [["0.55", "300"], ["0.53", "300"]]}}
        reads = []
        client = iu.PublicBooks(None, lambda tk: reads.append(tk) or book, pace=0)
        bot = iu.offline_bot(imm, client)
        now = datetime.now(timezone.utc)
        info = {"dollars_per_day": 50.0, "start": now - timedelta(days=1),
                "end": now + timedelta(days=6), "target": 100.0, "df": 0.5}
        m = {"ticker": t, "event_ticker": "KXZZUNQREAL-26NOV20", "status": "active",
             "close_time": _iso(now + timedelta(days=20)),
             "expected_expiration_time": _iso(now + timedelta(days=20)),
             "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.45",
             "volume_fp": "5000", "volume_24h_fp": "500"}
        x = iu.score_market(bot, imm, imm_quote_gaps.build_meta, t, info, m, now)
        self.assertEqual(x["screen"], "")
        self.assertGreater(x["est"][1.0], 0.0)
        self.assertGreater(x["est"][3.0], 1.5 * x["est"][1.0])
        self.assertEqual(reads, [t])                     # one public read for both sizes
        self.assertNotIn("KXZZUNQREAL", imm.SERIES_OVERRIDES)


class RollUpTests(unittest.TestCase):

    def mk(self, ev, x1, x3, days=2.0, screen=""):
        return {"t": ev + "-T", "ev": ev, "screen": screen, "days": days,
                "est": {} if screen else {1.0: x1, 3.0: x3}}

    def test_sums_totals_bar_counts_and_screens(self):
        r = iu.roll_up([self.mk("E1", 0.5, 1.5), self.mk("E1", 0.2, 0.6),
                        self.mk("E2", 1.0, 3.0), self.mk("E2", 0, 0, screen="one_sided")],
                       top_n=0, bar=1.3)
        self.assertEqual((r["x1"], r["x3"]), (1.7, 5.1))
        self.assertEqual((r["x1_total"], r["x3_total"]), (3.4, 10.2))
        self.assertEqual((r["x1_bar"], r["x3_bar"]), (1, 2))       # 2.0 and 3.0 / 6.0
        self.assertEqual((r["quotable"], r["screens"]), (3, Counter({"one_sided": 1})))

    def test_an_event_cap_keeps_its_best_markets(self):
        r = iu.roll_up([self.mk("E1", 0.5, 1.5), self.mk("E1", 0.2, 0.6), self.mk("E1", 0.9, 2.0)],
                       top_n=2, bar=1.3)
        self.assertEqual((r["x1"], r["x3"]), (1.4, 3.5))


class FakeReads:

    def __init__(self, settled=(), trades=None, series=None):
        self._settled, self._trades, self._series = list(settled), trades or {}, series or {}
        self.trade_calls = []

    def book(self, t):
        return {}

    def series(self, s):
        return self._series.get(s, {})

    def settled(self, s, since):
        return [m for m in self._settled if m["ticker"].startswith(s + "-")]

    def trades(self, t, max_ts, max_pages):
        self.trade_calls.append((t, max_ts))
        return [x for x in self._trades.get(t, []) if max_ts is None or iu._ts(x["created_time"]) <= max_ts]


def trade(px, n, taker, at):
    return {"yes_price_dollars": f"{px:.2f}", "count_fp": str(n), "taker_side": taker,
            "created_time": _iso(at)}


class TapeTests(unittest.TestCase):

    def test_makers_pnl_to_settlement_before_the_cutoff(self):
        # a day-dated event: the bot is out at 00:00 ET on Nov 03
        ev = "KXZZT-26NOV03"
        m = {"ticker": ev + "-T1", "event_ticker": ev, "result": "yes",
             "close_time": "2026-11-04T00:00:00Z", "expected_expiration_time": "2026-11-04T00:00:00Z",
             "volume_fp": "900"}
        before = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        after = datetime(2026, 11, 3, 15, tzinfo=timezone.utc)
        reads = FakeReads([m], {m["ticker"]: [
            trade(0.40, 100, "yes", before),       # maker sold YES at 40, settles 1: -60 x 100
            trade(0.20, 100, "no", before),        # maker bought YES at 20: +80 x 100
            trade(0.02, 500, "yes", before),       # 2c: outside 5-95, never counted
            trade(0.90, 1000, "yes", after),       # after the cutoff: not ours to lose
        ]})
        tp = iu.tape_read("KXZZT", NOW, imm, reads)
        self.assertEqual((tp["markets"], tp["events"], tp["of"]), (1, 1, 1))
        self.assertEqual(tp["cts"], 200.0)
        self.assertAlmostEqual(tp["pnl"], 20.0)                        # (-60 + 80) x 100 / 100
        self.assertAlmostEqual(iu.tape_cents(tp), 10.0)
        self.assertEqual(tp["cts_mid"], 100.0)                         # only the 40c trade is 25-75
        self.assertAlmostEqual(iu.tape_cents(tp, mid=True), -60.0)
        cut = imm.trade_cutoff_utc(ev, None, imm.parse_iso_utc(m["expected_expiration_time"]))
        self.assertEqual(reads.trade_calls, [(m["ticker"], int(cut.timestamp()))])
        self.assertIn("makers +10.0c/ct on 200 ct (25-75c -60.0c), 1 event", iu.tape_text(tp))

    def test_picks_the_busiest_market_of_each_event_first(self):
        ms = [{"ticker": f"KXQ-{e}-{i}", "event_ticker": f"KXQ-{e}", "volume_fp": str(v),
               "close_time": f"2026-10-{d}T00:00:00Z"}
              for e, d in (("A", 10), ("B", 20)) for i, v in ((1, 5), (2, 50), (3, 9))]
        got = [m["ticker"] for m in iu._pick_markets(ms, 3)]
        self.assertEqual(got, ["KXQ-B-2", "KXQ-A-2", "KXQ-B-3"])

    def test_texts(self):
        self.assertEqual(iu.tape_text(None), "tape not read yet")
        self.assertEqual(iu.tape_text({"days": 75, "cts": 0, "pnl": 0, "of": 0}),
                         "no settled trades in 75 days")
        self.assertTrue(iu.tape_thin({"cts": 1500, "events": 3}))
        self.assertTrue(iu.tape_thin({"cts": 5000, "events": 1}))
        self.assertFalse(iu.tape_thin({"cts": 5000, "events": 2}))


def row(**kw):
    r = {"series": "KXZZROW", "owner": "", "owner_bot": False, "scored": 3, "unread": 0,
         "quotable": 3, "screens": {}, "x1": 2.0, "x3": 5.0, "x1_bar": 2, "x3_bar": 3,
         "bar": 1.3, "title": "Monthly widget index", "frequency": "monthly",
         "category": "Economics", "tape": {"days": 75, "cts": 9000.0, "pnl": 45.0,
                                           "cts_mid": 0.0, "pnl_mid": 0.0, "events": 4}}
    r.update(kw)
    return r


def tp(c, cts=9000.0, events=4):
    return {"days": 75, "cts": cts, "pnl": c * cts / 100.0, "cts_mid": 0.0, "pnl_mid": 0.0,
            "events": events}


class CallTests(unittest.TestCase):

    def test_clean_scheduled_data_is_an_add(self):
        call, why = iu.call_for(row())
        self.assertEqual(call, "ADD")
        self.assertTrue(why.startswith("monthly public data, makers +0.5c/ct"), why)

    def test_owned_or_blocklisted_is_dont_quote(self):
        self.assertEqual(iu.call_for(row(owner="low_temp_trading.py trades the KXLOWT dailies",
                                         owner_bot=True)),
                         ("DON'T QUOTE", "low_temp_trading.py trades the KXLOWT dailies: "
                                         "quoting it would cross that bot"))
        self.assertEqual(iu.call_for(row(owner="blocklisted in incentive_mm.py")),
                         ("DON'T QUOTE", "blocklisted in incentive_mm.py"))

    def test_worth_adding_needs_a_dollar_a_day_at_x3(self):
        self.assertTrue(iu.promising(row(call="ADD WITH CARE", x3=1.0)))
        self.assertFalse(iu.promising(row(call="ADD WITH CARE", x3=0.24)))
        self.assertFalse(iu.promising(row(call="PASS", x3=50.0)))

    def test_nothing_quotable_or_under_the_bar_passes(self):
        self.assertEqual(iu.call_for(row(scored=0, quotable=0)),
                         ("PASS", "no paying market is open now (its program ended or its markets closed)"))
        self.assertEqual(iu.call_for(row(quotable=0, screens={"extreme_mid": 2, "one_sided": 1})),
                         ("PASS", "no two-sided book with a mid in 5-90c"))
        self.assertEqual(iu.call_for(row(quotable=0, screens={"cutoff": 3})),
                         ("PASS", "no market can quote now: past its cutoff"))
        self.assertEqual(iu.call_for(row(x3_bar=0, x3=0.9)),
                         ("PASS", "no market clears the $1.30 payout bar even at x3 (~$0.90/day)"))

    def test_one_offs_by_how_the_answer_arrives(self):
        c = iu.call_for(row(title="ChatGPT Release Date", frequency="one_off"))
        self.assertEqual(c[0], "PASS")
        self.assertIn('"Release"', c[1])
        self.assertEqual(iu.call_for(row(title="Next Bond actor", frequency="custom"))[0], "PASS")
        c = iu.call_for(row(title="Will the Senate repeal the tariff bill?", frequency="one_off"))
        self.assertEqual(c[0], "ADD WITH CARE")
        self.assertIn("slow public process", c[1])
        c = iu.call_for(row(title="Which of these state or local laws will pass?", frequency="one_off",
                            category="Politics", tape=tp(0.0, cts=0, events=0)))
        self.assertEqual(c[0], "ADD WITH CARE")                # "laws", the plural, too
        self.assertIn('("laws")', c[1])
        self.assertEqual(iu.call_for(row(title="Will Teslas be cheaper?", frequency="one_off"))[0],
                         "PASS")
        self.assertEqual(iu.call_for(row(title="Who wins the Swedish election?", frequency="one_off",
                                         category="Politics"))[0], "ADD WITH CARE")
        # "win" is a vote only where votes are: a TV show is not one
        self.assertEqual(iu.call_for(row(title="Who will win dancing with the stars?",
                                         frequency="one_off", category="Entertainment"))[0], "PASS")
        # an annual series takes the insider words too
        c = iu.call_for(row(title="Who will be charged with a federal crime", frequency="annual",
                            category="Politics"))
        self.assertEqual(c[0], "PASS")

    def test_a_live_feed_passes(self):
        c = iu.call_for(row(live="YouTube", title="Most YouTube views", frequency="weekly"))
        self.assertEqual(c, ("PASS", "a live feed settles it (YouTube): whoever watches it prices "
                                     "the market before us"))
        self.assertEqual(iu.live_source(imm, {"settlement_sources": [
            {"name": "BLS", "url": "https://bls.gov"},
            {"name": "YouTube", "url": "https://charts.youtube.com/charts"}]}), "YouTube")
        self.assertEqual(iu.live_source(imm, {"settlement_sources": [
            {"name": "BLS", "url": "https://bls.gov"}]}), "")

    def test_the_tape_decides_the_rest(self):
        self.assertEqual(iu.call_for(row(tape=tp(-6.3)))[0], "PASS")
        self.assertIn("adversely selected", iu.call_for(row(tape=tp(-6.3)))[1])
        # the tape's evidence is the reason before a live source's
        self.assertIn("adversely selected", iu.call_for(row(tape=tp(-6.3), live="ESPN"))[1])
        # a heavy loss passes on one event too: KXNFLTRADE, -10.5c on 17k ct
        self.assertEqual(iu.call_for(row(tape=tp(-10.5, cts=17000, events=1)))[0], "PASS")
        self.assertEqual(iu.call_for(row(tape=tp(-3.0)))[0], "ADD WITH CARE")
        c = iu.call_for(row(tape=tp(-9.0, cts=800)))          # too thin to pass on
        self.assertEqual(c[0], "ADD WITH CARE")
        self.assertIn("public data, but too little settled tape to judge", c[1])
        self.assertEqual(iu.call_for(row(tape=None)), ("ADD WITH CARE", "its tape is not read yet"))
        self.assertEqual(iu.call_for(row(category="Mentions", tape=tp(-3.0)))[0], "ADD WITH CARE")

    def test_an_event_market_needs_tape_to_be_promising(self):
        ev = dict(title="Pac-12 champion", frequency="custom", category="Sports")
        c = iu.call_for(row(tape=tp(1.0, cts=900, events=1), **ev))
        self.assertEqual(c[0], "PASS")
        self.assertIn("an event market with too little settled tape", c[1])
        c = iu.call_for(row(tape=tp(1.7), **ev))
        self.assertEqual(c[0], "ADD WITH CARE")
        self.assertIn("an event market, but makers held up", c[1])
        # economic data that is annual is still data
        self.assertEqual(iu.call_for(row(frequency="annual", tape=tp(0.4)))[0], "ADD")

    def test_what_adding_takes(self):
        self.assertEqual(iu.needs_for(row()),
                         "x1: add it to extra_allow_series.json (hot-loaded); "
                         "x3: a code override (size_mult, as KXTRUMPVH)")
        self.assertEqual(iu.needs_for(row(x1_bar=0)),
                         "x3 only (no market clears the $1.30 bar at x1): a code override "
                         "(size_mult, as KXTRUMPVH)")
        self.assertIn("a stop time", iu.needs_for(row(undated=True, x3=2.2)))
        self.assertIn("election day row", iu.needs_for(row(category="Elections")))


LOG = """\
2026-10-08 02:20:26Z [WATCH] alert:
IMM watch: 2 dark item(s) worth ~$16/day need a row or a call

- ~$11.00/day, dark 1.0h  KXHURCAT: newly paying ($500/day pool) and not on the allowlist
    fix: your call: enroll the family, or leave it out
- ~$5.00/day, dark 1.0h  KXSPAINTURNOUT-26NOV29: KXSPAINTURNOUT-26NOV29 has no verified election day
    fix: by hand
2026-10-08 02:20:26Z [IMM-WATCH] alerts -> someone
2026-10-20 00:20:27Z [WATCH] alert:
IMM watch: 1 not quoted (~$15/day)

- NOT QUOTED (not in the bot's book) -- missing a place on the allowlist (a new family: your call). ~$15.20/day, ~$78.94 over the rest of its program; flagged 1.0h ago  KXSWEDENPMVOTE: newly paying ($192/day pool) and not on the allowlist
    what it is: Swedish Prime Minister approval vote (Politics, one_off)
2026-10-20 00:20:28Z [WATCH] enrolled 0
2026-10-30 03:50:28Z [WATCH] alert:
KXBOND: Next Bond actor (Entertainment, custom)
  Call:   DON'T QUOTE: someone decides or announces it ("Next"): insiders know first
  Now:    not quoted for 30 min; needs a place on the allowlist (a new family)
  Worth:  ~$7.59/day; ~$46.06 over the 6.1 days left (ends Nov 05 05:00Z)
  Fix:    your call: enroll the family, or leave it out

KXTRUMPMENTIONB-26NOV02: Trump mentions
  Call:   KEEP QUOTING: a mention event
  Now:    quoted on 20 of 25 markets; needs its start time
  Worth:  ~$9.00/day

2026-10-30 03:50:28Z [IMM-WATCH] alerts -> someone
2026-10-31 03:50:28Z [WATCH] alert:
KXBOND: Next Bond actor (Entertainment, custom)
  Call:   QUOTE: a second alert must not win
  Now:    not quoted for 30 min; needs a place on the allowlist (a new family)
  Worth:  ~$99.00/day
2026-10-31 03:50:29Z [IMM-WATCH] alerts -> someone
"""


class WatchTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.log = os.path.join(self.dir, "family-watch.log")
        with open(self.log, "w", encoding="utf-8") as f:
            f.write(LOG)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_every_alert_format_is_read_and_the_first_alert_wins(self):
        a = iu.watch_alerts(self.log)
        self.assertEqual(sorted(a), ["KXBOND", "KXHURCAT", "KXSWEDENPMVOTE"])
        self.assertEqual(a["KXHURCAT"], {"at": "2026-10-08", "est": 11.0, "call": ""})
        self.assertEqual(a["KXSWEDENPMVOTE"]["est"], 15.2)
        self.assertEqual(a["KXBOND"], {"at": "2026-10-30", "est": 7.59, "call": "DON'T QUOTE"})
        self.assertEqual(iu.watch_alerts(os.path.join(self.dir, "none.log")), {})

    def test_status_lines(self):
        a = iu.watch_alerts(self.log)
        st = {"baseline_not_allowed": ["KXOLD"], "baseline_at": "2026-10-08T00:50:27+00:00",
              "items": {"new:KXSMALL": {"est": 1.25}}}
        self.assertEqual(iu.watch_status("KXBOND", st, a, 4.0),
                         "watch emailed it 10/30 at ~$7.59/day (DON'T QUOTE)")
        self.assertEqual(iu.watch_status("KXBOND", st, a, 20.0),
                         "watch emailed it 10/30 at ~$7.59/day; it has grown to ~$20.00 (DON'T QUOTE)")
        self.assertEqual(iu.watch_status("KXSMALL", st, a, 1.0),
                         "watch tracks it under its $5/day alert bar (~$1.25/day)")
        self.assertEqual(iu.watch_status("KXOLD", st, a, 1.0),
                         "never emailed: it was paying when the watch began (10/08)")
        self.assertEqual(iu.watch_status("KXNEW", st, a, 1.0), "not emailed by the watch")


class FakeBot:
    """The offline bot's surface score_all uses: programs, the allow test,
    the screen, and an estimator that reads the x3 override like the real
    one (applied_mention_mult)."""

    def __init__(self, programs, allowed=(), screens=None, per=None):
        self.programs, self.allowed = programs, set(allowed)
        self.screens, self.per = screens or {}, per or {}
        self.client = SimpleNamespace(reads=0)
        self._yield_boost = {"stale": 1.0}

    def fetch_programs(self):
        return self.programs

    def _allowed(self, t):
        return imm.series_of(t) in self.allowed

    def _screen(self, meta, now):
        return self.screens.get(meta.ticker)

    def _estimate_candidate_yield(self, meta, own):
        assert not self._yield_boost, "an armed size mode leaked into the estimate"
        ov = imm.SERIES_OVERRIDES.get(meta.series)
        meta.floor_dollars_per_day = self.per.get(meta.ticker, 0.0) * (
            ov.size_mult if ov is not None and ov.size_mult else 1.0)
        self._yield_boost["armed"] = 1.0
        return True


class ScoreAllTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "unquoted_tape.json")
        self.wlog = os.path.join(self.dir, "family-watch.log")
        self.wstate = os.path.join(self.dir, "family_watch_state.json")
        with open(self.wlog, "w", encoding="utf-8") as f:
            f.write(LOG)
        with open(self.wstate, "w", encoding="utf-8") as f:
            json.dump({"baseline_not_allowed": ["KXZZW"], "baseline_at": "2026-10-08T00:00:00Z"}, f)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, **kw):
        info = {"dollars_per_day": 40.0, "start": NOW - timedelta(days=1),
                "end": NOW + timedelta(days=4), "target": 100.0, "df": 0.5}
        progs = {t: dict(info) for t in ("KXZZW-26NOV20-A", "KXZZW-26NOV20-B", "KXZZS-26NOV20-A",
                                         "KXZZE-26NOV20-A", "KXZZX-26NOV20-A")}
        bot = FakeBot(progs, allowed={"KXZZE"}, screens={"KXZZW-26NOV20-B": "one_sided"},
                      per={"KXZZW-26NOV20-A": 1.0, "KXZZS-26NOV20-A": 0.1, "KXZZX-26NOV20-A": 0.8})
        details = {t: {"ticker": t, "status": "active"} for t in progs}
        gaps_mod = SimpleNamespace(bulk_market_details=lambda client, tks: {t: details[t] for t in tks})

        def build_meta(b, t, inf, m, now):
            return SimpleNamespace(ticker=t, event_ticker=t.rsplit("-", 1)[0], series=imm.series_of(t),
                                   cutoff=now + timedelta(days=10), close_time=None,
                                   program_end=inf["end"], floor_dollars_per_day=0.0)

        settled = [{"ticker": "KXZZW-26OCT01-A", "event_ticker": "KXZZW-26OCT01", "result": "no",
                    "close_time": "2026-10-02T00:00:00Z", "volume_fp": "10"},
                   {"ticker": "KXZZW-26OCT08-A", "event_ticker": "KXZZW-26OCT08", "result": "no",
                    "close_time": "2026-10-09T00:00:00Z", "volume_fp": "10"}]
        at = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        trades = {m["ticker"]: [trade(0.30, 2000, "yes", at)] for m in settled}  # makers sold the loser
        reads = FakeReads(settled, trades, series={
            "KXZZW": {"title": "Weekly widget count", "category": "Economics", "frequency": "weekly"},
            "KXZZS": {"title": "Small thing", "frequency": "monthly"},
            "KXZZX": {"title": "Will the CEO be fired?", "frequency": "one_off"}})
        rows = [gap("KXZZW", "KXZZW-26NOV20", 80.0, n=2), gap("KXZZS", "KXZZS-26NOV20", 40.0),
                gap("KXZZE", "KXZZE-26NOV20", 40.0), gap("KXZZX", "KXZZX-26NOV20", 40.0),
                gap("KXZZGONE", "KXZZGONE-26NOV20", 10.0)]
        args = dict(imm=imm, gaps_mod=gaps_mod, bot=bot, reads=reads, cache_path=self.cache,
                    watch_state_path=self.wstate, watch_log_path=self.wlog, build_meta=build_meta)
        args.update(kw)
        return iu.score_all(rows, NOW, **args), reads

    def test_scores_calls_and_drops_the_enrolled(self):
        res, reads = self._run()
        by = {r["series"]: r for r in res["rows"]}
        self.assertEqual(res["enrolled"], ["KXZZE"])
        self.assertEqual([r["series"] for r in res["rows"]], ["KXZZW", "KXZZX", "KXZZS", "KXZZGONE"])
        w = by["KXZZW"]
        self.assertEqual((w["quotable"], w["screens"]), (1, {"one_sided": 1}))
        self.assertEqual((w["x1"], w["x3"], w["x1_total"], w["x1_bar"]), (1.0, 3.0, 4.0, 1))
        self.assertEqual(w["call"], "ADD")
        self.assertIn("weekly public data, makers +30.0c/ct on 4.0k ct", w["why"])
        self.assertIn("x1: add it to extra_allow_series.json", w["needs"])
        self.assertEqual(w["watch"], "never emailed: it was paying when the watch began (10/08)")
        self.assertEqual(by["KXZZX"]["call"], "PASS")                  # an insider one-off
        self.assertEqual(by["KXZZX"]["needs"], "")
        self.assertEqual(by["KXZZS"]["call"], "PASS")                  # under the bar at x3
        self.assertEqual(by["KXZZGONE"]["call"], "PASS")
        self.assertEqual(res["ctx"]["tape_reads"], 1)                  # only the series that needs it
        self.assertEqual([t for t, _ in reads.trade_calls], ["KXZZW-26OCT08-A", "KXZZW-26OCT01-A"])
        self.assertNotIn("KXZZW", imm.SERIES_OVERRIDES)

    def test_the_cache_serves_the_next_pass_and_the_budget_holds_tapes_back(self):
        res, reads = self._run(tape_budget=0)
        w = {r["series"]: r for r in res["rows"]}["KXZZW"]
        self.assertEqual((w["call"], w["why"]), ("ADD WITH CARE", "its tape is not read yet"))
        self.assertEqual((res["ctx"]["tape_reads"], res["ctx"]["tape_pending"]), (0, 1))
        self._run()                                                    # reads and caches it
        res, reads = self._run()
        self.assertEqual(reads.trade_calls, [])                        # served from the cache
        self.assertEqual({r["series"]: r for r in res["rows"]}["KXZZW"]["call"], "ADD")
        with open(self.cache, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["meta"]["KXZZW"]["title"], "Weekly widget count")

    def test_a_book_budget_leaves_the_series_partial(self):
        # the biggest pool reads first; a screened market costs no read; the
        # insider one-off, whose call no estimate can change, reads last
        res, _ = self._run(max_books=2)
        by = {r["series"]: r for r in res["rows"]}
        self.assertEqual(res["ctx"]["books"], 2)
        self.assertEqual({s: by[s]["partial"] for s in ("KXZZW", "KXZZS", "KXZZX")},
                         {"KXZZW": False, "KXZZS": False, "KXZZX": True})
        self.assertEqual(by["KXZZW"]["screens"], {"one_sided": 1})
        self.assertEqual(by["KXZZX"]["call"], "PASS")


class WeeklyEmailTests(unittest.TestCase):

    def res(self):
        def r(series, call, why, x1, x3, **kw):
            d = {"series": series, "title": series.title(), "call": call, "why": why, "x1": x1,
                 "x3": x3, "pool": 100.0, "n": 4, "x1_bar": 1, "x3_bar": 2, "bar": 1.3,
                 "quotable": 2, "needs": "x1: add it" if call.startswith("ADD") else "",
                 "watch": "not emailed by the watch", "tape": None, "category": "Economics",
                 "frequency": "weekly"}
            d.update(kw)
            return d
        return {"rows": [r("KXBIG", "PASS", "someone decides or announces it", 9.0, 20.0,
                           watch="watch emailed it 10/30 at ~$7.59/day", watch_alerted=True),
                         r("KXADD", "ADD", "weekly public data, makers +1.0c/ct", 4.0, 11.0),
                         r("KXCARE", "ADD WITH CARE", "a mention market", 1.0, 2.5,
                           tape={"days": 75, "cts": 0, "pnl": 0, "of": 0}),
                         r("KXTINY", "PASS", "under the bar", 0.1, 0.3),
                         r("KXTINYADD", "ADD WITH CARE", "a slow public process", 0.08, 0.24),
                         r("KXDARK", "PASS", "no two-sided book", 0.0, 0.0, quotable=0)],
                "enrolled": ["KXSODAARRAY"], "ctx": {"books": 12, "tape_reads": 1, "secs": 40}}

    def test_ranked_by_x1_with_calls_and_tails(self):
        import imm_unquoted_weekly as w
        now = NOW.timestamp()
        subject, body = w.build_email(self.res(), now - 3600, now)
        self.assertEqual(subject, "IMM unquoted weekly: 1 ADD, 1 ADD WITH CARE (~$5/day at x1, ~$14 at x3)")
        lines = body.splitlines()
        heads = [ln for ln in lines if ln[:1].isdigit()]
        self.assertEqual([h.split(":")[0] for h in heads], ["1. KXADD", "2. KXCARE"])
        self.assertIn("WORTH ADDING (2), by x1 $/day", lines)
        self.assertIn("   ADD: weekly public data, makers +1.0c/ct", lines)
        self.assertIn("   x1 $4.00/day, x3 $11.00/day; pool $100/day; 1 of 4 markets clear the $1.30 bar "
                      "at x1, 2 at x3", lines)
        self.assertIn("   Takes: x1: add it", lines)
        self.assertIn("no settled trades in 75 days", body)      # a tape the reason does not carry
        self.assertIn("PASSED, $1/day or more at x3 (1), by x1 $/day", lines)
        self.assertIn("- KXBIG: Kxbig -- PASS: someone decides or announces it. x1 $9.00/day, x3 $20.00, "
                      "pool $100/day; watch emailed it 10/30 at ~$7.59/day", lines)
        # a call worth adding on under $1/day at x3 is named, not proposed
        self.assertIn("Under $1/day even at x3 (2): KXTINY $0.30, KXTINYADD $0.24 (ADD WITH CARE)",
                      body)
        self.assertIn("Nothing quotable now (1): KXDARK", body)
        self.assertIn("Enrolled since the gap list was built, left out (1): KXSODAARRAY", body)
        self.assertIn("(12 books and 1 tape read in 40s.)", body)
        self.assertNotIn("old: the dashboard", body)
        _, body = w.build_email(self.res(), now - 8 * 3600, now)
        self.assertIn("(8h old: the dashboard's Kalshi reads may be failing)", body)
        with mock.patch.object(w, "PASSED_SHOW", 0):
            body = w.build_email(self.res(), now, now)[1]
        self.assertIn("- also passed (1): KXBIG", body.splitlines())

    def test_nothing_to_add(self):
        import imm_unquoted_weekly as w
        res = {"rows": [r for r in self.res()["rows"] if r["call"] == "PASS"], "ctx": {}}
        subject, body = w.build_email(res, NOW.timestamp(), NOW.timestamp())
        self.assertEqual(subject, "IMM unquoted weekly: nothing to add (3 series scored)")
        self.assertIn("Nothing is worth adding this week.", body)

    def test_dry_sends_nothing_and_a_sent_day_is_skipped(self):
        import imm_unquoted_weekly as w
        d = tempfile.mkdtemp()
        try:
            with mock.patch.object(w, "load_gaps", return_value=([], NOW.timestamp())), \
                    mock.patch.object(w.iu, "score_all", return_value=self.res()) as sa, \
                    mock.patch.object(w.imm, "STATUS_DIR", d), \
                    mock.patch.object(w.imm, "Alerter") as al:
                self.assertEqual(w.main(["--dry"]), 0)
                al.assert_not_called()
                self.assertEqual(os.listdir(d), [])
                al.return_value.enabled = True
                al.return_value.send_message.return_value = True
                self.assertEqual(w.main([]), 0)
                self.assertEqual(al.return_value.send_message.call_count, 1)
                self.assertEqual(len([f for f in os.listdir(d) if f.endswith(".marker")]), 1)
                self.assertEqual(w.main([]), 0)                       # sent today: skipped
                self.assertEqual(al.return_value.send_message.call_count, 1)
                self.assertEqual(sa.call_count, 2)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class DashboardTests(unittest.TestCase):

    def test_a_new_launch_the_scorer_passes_is_not_promising(self):
        import imm_dashboard as dash
        now = NOW.timestamp()

        def ev(ser):
            return {"ev": ser + "-26NOV20", "ser": ser, "fam": "Other", "grp": "", "what": "",
                    "n": 2, "pool": 240.0, "pool_total": 240.0, "start": now - 3600,
                    "end": now + 3 * 86400, "launched": now - 3600, "bot": "not allowed",
                    "q": 0, "sel": 0, "est_now": 0.0, "series_first": now - 3600,
                    "g_est": 8.0, "g_yld": 0.03, "g_reason": "not in allowlist", "g_n": 2,
                    "g_nest": 2, "g_partial": False}
        calls = {"KXP": {"call": "PASS", "why": "insiders know first", "ok": False},
                 "KXA": {"call": "ADD", "why": "weekly public data", "ok": True}}
        g = {x["ser"]: x for x in dash.new_event_groups([ev("KXP"), ev("KXA"), ev("KXN")],
                                                         now - 86400, now, calls)}
        self.assertEqual((g["KXP"]["flag"], g["KXP"]["call"]), ("", "PASS"))
        self.assertEqual((g["KXA"]["flag"], g["KXA"]["call"]), ("promising", "ADD"))
        self.assertEqual(g["KXN"]["flag"], "promising")               # not scored: as before
        self.assertNotIn("call", g["KXN"])

    def test_fetch_unquoted_adds_the_family_and_the_tape_cents(self):
        import imm_dashboard as dash
        res = {"rows": [{"series": "KXTRUMPAPPROVEX", "category": "Politics", "call": "ADD", "x3": 2.0,
                         "tape": {"days": 75, "cts": 1000.0, "pnl": -20.0, "events": 3}}],
               "enrolled": [], "ctx": {}}
        with mock.patch.object(iu, "score_all", return_value=res) as sa:
            out = dash.fetch_unquoted(None, imm, NOW, [gap("KXA", "KXA-1", 1.0)])
        self.assertEqual(sa.call_args.kwargs["max_books"], dash.UNQUOTED_MAX_BOOKS)
        self.assertEqual(sa.call_args.kwargs["tape_budget"], dash.UNQUOTED_TAPE_SERIES)
        r = out["rows"][0]
        self.assertEqual(r["fam"], dash.family_of("KXTRUMPAPPROVEX", "Politics")[0])
        self.assertAlmostEqual(r["tape_c"], -2.0)
        self.assertTrue(r["promising"])
        with self.assertRaises(RuntimeError):
            dash.fetch_unquoted(None, imm, NOW, [])


if __name__ == "__main__":
    unittest.main()
