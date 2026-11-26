#!/usr/bin/env python3
"""Tests for imm_upcoming.py and the new-programs email's upcoming section
(Jack 2026-11-12: "yes add 'upcoming incentive markets' to the morning
new-programs email, ranked by attractiveness and whether we are expecting to
quote them or not").

Run: python -m unittest test_imm_upcoming

What matters: the verdicts (a blocked or not-allowlisted series is "no", an
allowed one clearing the floors is "yes", a wide book today is "maybe"), a
market paying now is a renewal and not listed, the ranking is by estimate,
and an extreme-mid strike neither earns nor inherits its series' capture.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import imm_upcoming as up                                            # noqa: E402
import incentive_mm as imm                                           # noqa: E402
import send_imm_new_programs as np                                   # noqa: E402

NOW = datetime(2026, 11, 13, 10, 45, tzinfo=timezone.utc)
START = NOW + timedelta(hours=6)
END = START + timedelta(days=6)


def setUpModule():
    tmp = tempfile.mkdtemp(prefix="imm_upcoming_test_")
    imm.STATUS_DIR = tmp
    imm.ALERT_RECIPIENTS = []


def prog(t, reward=600000, start=START, end=END):
    f = "%Y-%m-%dT%H:%M:%SZ"
    return {"market_ticker": t, "incentive_type": "liquidity", "period_reward": reward,
            "target_size_fp": "500.00", "discount_factor_bps": 5000, "paid_out": False,
            "start_date": start.strftime(f), "end_date": end.strftime(f)}


class FakeBot:
    """The bot surface imm_upcoming uses: admission, screens, the estimator."""

    upcoming_by_market = staticmethod(imm.IncentiveMarketMaker.upcoming_by_market)

    def __init__(self, allowed=(), blocked=(), screens=None, est=None):
        self.allowed, self.blocked = set(allowed), set(blocked)
        self.screens = screens or {}
        self.est = est or {}
        self.estimated = []

    def _blocked(self, t):
        return imm.series_of(t) in self.blocked

    def _allowed(self, t):
        return imm.series_of(t) in self.allowed

    def _screen(self, meta, at):
        return self.screens.get(meta.ticker)

    def _estimate_candidate_yield(self, meta, _ex):
        self.estimated.append(meta.ticker)
        v = self.est.get(meta.ticker)
        if v is None:
            return False
        meta.est_dollars_per_day = v
        meta.floor_dollars_per_day = v
        meta.yield_per_contract = 0.01 if v > 0 else 0.0
        return True


def _meta(bot, t, info, m, at):
    return SimpleNamespace(ticker=t, series=imm.series_of(t), event_ticker=t.rsplit("-", 1)[0],
                           program_end=info["end"], cutoff=None, close_time=None,
                           est_dollars_per_day=0.0, floor_dollars_per_day=0.0,
                           yield_per_contract=0.0)


def run(rows, bot, active=(), markets=None, max_books=None, waves=None, meta=_meta):
    tickers = {p["market_ticker"] for p in rows}
    markets = markets if markets is not None else {
        t: {"ticker": t, "status": "active"} for t in tickers}
    with mock.patch.object(up.gaps, "bulk_market_details",
                           lambda c, ts: {t: markets[t] for t in ts if t in markets}), \
            mock.patch.object(up.gaps, "build_meta", meta), \
            mock.patch.object(up, "hot_reload", lambda: None), \
            mock.patch.object(imm, "ensure_family_override", lambda s: None), \
            mock.patch.object(imm, "curated_event", lambda *a: False), \
            mock.patch.object(imm, "series_min_est_rate", lambda s: 0.0), \
            mock.patch.object(imm, "floor_bar_dollars", lambda s, b: 1.10), \
            mock.patch.object(imm, "NO_NEW_SERIES", set()), \
            mock.patch.object(imm, "scan_universe_reason", lambda t: "not_scan"), \
            mock.patch.object(imm, "series_family_suffix", lambda s: False):
        # no registry waves unless a test names them (never the live waves.json)
        return up.assess(None, bot, rows, set(active), NOW, max_books=max_books,
                         waves={} if waves is None else waves)


class Verdicts(unittest.TestCase):
    def test_yes_maybe_no_and_the_renewal(self):
        rows = [prog("KXGOODS-26DEC01-T1"), prog("KXGOODS-26DEC01-T2"),
                prog("KXWIDES-26DEC01-T1"),
                prog("KXSHUT-26DEC01-T1"), prog("KXNOPE-26DEC01-T1"),
                prog("KXLIVE-26DEC01-T1")]
        bot = FakeBot(allowed={"KXGOODS", "KXWIDES", "KXSHUT", "KXLIVE"},
                      blocked={"KXSHUT"},
                      screens={"KXWIDES-26DEC01-T1": "wide"},
                      est={"KXGOODS-26DEC01-T1": 5.0, "KXGOODS-26DEC01-T2": 0.01,
                           "KXWIDES-26DEC01-T1": 2.0, "KXNOPE-26DEC01-T1": 1.0})
        res = run(rows, bot, active={"KXLIVE-26DEC01-T1"})
        by = {e["series"]: e for e in res["rows"]}
        self.assertNotIn("KXLIVE", by)                        # paying now: a renewal
        self.assertEqual(res["renewals"][0], 1)
        g = by["KXGOODS"]
        self.assertEqual((g["kind"], g["n_yes"], g["n_no"]), ("yes", 1, 1))
        self.assertEqual(up.quote_cell(g), "yes 1/2; 1 no (under payout floor)")
        self.assertEqual(by["KXWIDES"]["kind"], "maybe")
        self.assertEqual(up.quote_cell(by["KXWIDES"]), "maybe 1/1 (book wide today)")
        self.assertEqual(up.quote_cell(by["KXSHUT"]), "no (blocked (config))")
        self.assertEqual(up.quote_cell(by["KXNOPE"]), "no (not allowlisted)")
        # ranked by estimate: 6 days x $5 (+ 0.06) first
        self.assertEqual(res["rows"][0]["series"], "KXGOODS")
        self.assertAlmostEqual(g["est_total"], 6 * 5.01, places=6)

    def test_extreme_mid_strikes_neither_earn_nor_inherit(self):
        rows = [prog(f"KXFTS-26DEC08-T{i}") for i in range(6)]
        screens = {f"KXFTS-26DEC08-T{i}": "extreme_mid" for i in range(2, 6)}
        bot = FakeBot(allowed={"KXFTS"}, screens=screens,
                      est={"KXFTS-26DEC08-T0": 4.0, "KXFTS-26DEC08-T1": 2.0})
        e = run(rows, bot)["rows"][0]
        self.assertAlmostEqual(e["est_total"], 6 * 6.0)       # the two live strikes only
        self.assertEqual(e["est_src"], "")
        self.assertEqual(sorted(bot.estimated), ["KXFTS-26DEC08-T0", "KXFTS-26DEC08-T1"])
        self.assertEqual(up.quote_cell(e), "yes 2/6; 4 no (extreme mid)")

    def test_unread_markets_take_the_series_capture(self):
        rows = [prog(f"KXBIG-26DEC01-T{i}") for i in range(4)]
        bot = FakeBot(allowed={"KXBIG"}, est={"KXBIG-26DEC01-T0": 3.0, "KXBIG-26DEC01-T1": 3.0})
        with mock.patch.object(up, "MAX_PER_SERIES", 2):
            e = run(rows, bot)["rows"][0]
        self.assertEqual(e["est_src"], "~")
        self.assertAlmostEqual(e["est_total"], 4 * 6 * 3.0)
        self.assertEqual(up.quote_cell(e), "yes 2/4; 2 maybe (not estimated (book budget))")

    def test_a_series_never_read_takes_the_proxy(self):
        rows = [prog("KXOFF-26DEC01-T1", reward=1000000)]     # $100 pool
        e = run(rows, FakeBot())["rows"][0]
        self.assertEqual((e["kind"], e["est_src"]), ("no", "proxy"))
        self.assertAlmostEqual(e["est_total"], 100 * up.PROXY_CAPTURE)

    def test_quotable_markets_get_the_book_budget_first(self):
        rows = [prog("KXHUGEOFF-26DEC01-T1", reward=90000000), prog("KXSMALL-26DEC01-T1")]
        bot = FakeBot(allowed={"KXSMALL"},
                      est={"KXSMALL-26DEC01-T1": 1.0, "KXHUGEOFF-26DEC01-T1": 1.0})
        with mock.patch.object(up, "MAX_BOOKS", 1):
            run(rows, bot)
        self.assertEqual(bot.estimated, ["KXSMALL-26DEC01-T1"])

    def test_a_market_not_open_yet_is_a_maybe(self):
        rows = [prog("KXLATER-26DEC01-T1")]
        e = run(rows, FakeBot(allowed={"KXLATER"}),
                markets={"KXLATER-26DEC01-T1": {"status": "initialized"}})["rows"][0]
        self.assertEqual(up.quote_cell(e), "maybe 1/1 (market not open yet (initialized))")

    def test_a_series_rolls_its_events_into_one_row(self):
        rows = [prog("KXFX-26NOV17-T1"), prog("KXFX-26NOV18-T1"), prog("KXFX-26NOV19-T1")]
        e = run(rows, FakeBot())["rows"][0]
        self.assertEqual((e["n"], e["label"]), (3, "KXFX x3 events"))

    def test_the_cheap_read_reads_no_book_and_returns_every_market(self):
        # 2026-11-18 (the family watch's UPCOMING ALERTS): max_books=0 runs the
        # config and screen verdicts only; each market comes back with its own
        rows = [prog("KXGOODS-26DEC01-T1"), prog("KXDARK-26DEC01-T1"),
                prog("KXNOPE-26DEC01-T1")]
        bot = FakeBot(allowed={"KXGOODS", "KXDARK"},
                      screens={"KXDARK-26DEC01-T1": "cutoff"},
                      est={"KXGOODS-26DEC01-T1": 5.0})
        res = run(rows, bot, max_books=0)
        self.assertEqual((bot.estimated, res["books"]), ([], 0))
        m = res["markets"]
        self.assertEqual((m["KXDARK-26DEC01-T1"]["kind"], m["KXDARK-26DEC01-T1"]["why"]),
                         ("no", "cutoff at or before the start"))
        self.assertEqual(m["KXNOPE-26DEC01-T1"]["why"], "not allowlisted")
        self.assertEqual((m["KXGOODS-26DEC01-T1"]["kind"], m["KXGOODS-26DEC01-T1"]["why"]),
                         ("maybe", "not estimated (book budget)"))
        self.assertEqual(m["KXGOODS-26DEC01-T1"]["info"]["start"], START)
        # the default still reads the books
        self.assertGreater(run(rows, FakeBot(allowed={"KXGOODS"},
                                             est={"KXGOODS-26DEC01-T1": 5.0}))["books"], 0)

    def test_every_reload_is_a_bot_loader_and_the_writers_series_lists_load(self):
        # 2026-11-20: KXSNOWTHANKSGIVING read "not allowlisted" here while the
        # bot had it from the weekend writer's day_series
        for name in up._RELOADS:
            self.assertTrue(callable(getattr(imm, name, None)), name)
        for name in ("load_rain_wknd", "load_snow_days", "load_resort_snow", "load_hight_fair"):
            self.assertIn(name, up._RELOADS)


class WaveRelight(unittest.TestCase):
    """2026-11-26: the 621 KXDPRESPRIMARY markets of w1111-elec2028's relight
    read "cutoff at or before the start" because their list entries still
    carried last period's stop. A wave series' relit market is judged on the
    entry the wave's relist writes: the same entry, the stop at its program's
    end (or the wave's stop cap)."""

    T = "KXWAVEP-28AK-ABC"
    OLD = NOW - timedelta(days=1)                   # last period's stop, passed

    class CutoffBot(FakeBot):
        def _screen(self, meta, at):
            return "cutoff" if meta.cutoff is not None and at >= meta.cutoff else None

    def setUp(self):
        self.seen = []

        def meta(bot, t, info, m, at):
            # the election-series rule: a listed market's stop IS its cutoff
            e = imm.PVD_MARKETS.get(t)
            self.seen.append(e["stop"] if e else None)
            return SimpleNamespace(**dict(vars(_meta(bot, t, info, m, at)),
                                          cutoff=e["stop"] if e else None))
        self.meta = meta
        self.entry = {"stop": self.OLD, "pmin": 1, "pmax": 99, "event": "KXWAVEP-28AK",
                      "bsize": 4500, "asize": 300}
        patcher = mock.patch.dict(imm.PVD_MARKETS, {self.T: dict(self.entry)}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, waves, **kw):
        return run([prog(self.T)], self.CutoffBot(allowed={"KXWAVEP"}), max_books=0,
                   waves=waves, meta=self.meta, **kw)

    def test_a_relit_market_is_judged_on_the_stop_its_wave_writes(self):
        res = self._run({"KXWAVEP": None})
        r = res["markets"][self.T]
        self.assertEqual((r["kind"], r["why"]), ("maybe", "not estimated (book budget)"))
        self.assertEqual(res["relisted"], [self.T])
        self.assertEqual(self.seen, [END])                 # the program's end
        # ...and the bot's list is put back
        self.assertEqual(imm.PVD_MARKETS[self.T], self.entry)

    def test_without_its_wave_the_passed_stop_still_reads_dark(self):
        res = self._run({})
        r = res["markets"][self.T]
        self.assertEqual((r["kind"], r["why"]), ("no", "cutoff at or before the start"))
        self.assertEqual(res["relisted"], [])

    def test_the_waves_stop_cap_bounds_the_new_stop(self):
        cap = START + timedelta(days=1)
        self._run({"KXWAVEP": cap})
        self.assertEqual(self.seen, [cap])
        # a cap at or before the start relists nothing
        self.seen.clear()
        res = self._run({"KXWAVEP": START})
        self.assertEqual((res["relisted"], self.seen), ([], [self.OLD]))

    def test_a_live_entry_and_a_paying_market_are_left_alone(self):
        imm.PVD_MARKETS[self.T]["stop"] = START + timedelta(hours=1)  # listed through the start
        self.assertEqual(self._run({"KXWAVEP": None})["relisted"], [])
        imm.PVD_MARKETS[self.T]["stop"] = self.OLD
        res = self._run({"KXWAVEP": None}, active={self.T})          # a renewal
        self.assertEqual(res["relisted"], [])
        # an unlisted market of a wave series is judged unlisted
        del imm.PVD_MARKETS[self.T]
        self.assertEqual(self._run({"KXWAVEP": None})["relisted"], [])

    def test_the_list_is_put_back_when_the_judging_fails(self):
        class Boom(self.CutoffBot):
            def _screen(self, meta, at):
                raise RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            run([prog(self.T)], Boom(allowed={"KXWAVEP"}), max_books=0,
                waves={"KXWAVEP": None}, meta=self.meta)
        self.assertEqual(imm.PVD_MARKETS[self.T], self.entry)

    def test_wave_relists_reads_enabled_waves_main_and_near(self):
        cap = "2026-12-01T05:00:00Z"
        data = {"waves": {
            "w-a": {"enabled": True, "series": ["KXA", "KXB"], "near": ["KXN"], "stop_cap": None},
            "w-b": {"enabled": True, "series": ["KXB"], "stop_cap": cap},
            "w-off": {"enabled": False, "series": ["KXOFF"]}}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(data, f)
        self.addCleanup(os.unlink, f.name)
        got = up.wave_relists(f.name)
        self.assertEqual(set(got), {"KXA", "KXB", "KXN"})
        self.assertIsNone(got["KXA"])
        self.assertEqual(got["KXB"], imm.parse_iso_utc(cap))   # the earlier cap
        self.assertEqual(up.wave_relists(f.name + ".missing"), {})


class EmailSection(unittest.TestCase):
    def _ctx(self, upcoming):
        return {"upcoming": upcoming}

    def _up(self):
        rows = [prog("KXGOODS-26DEC01-T1"), prog("KXNOPE-26DEC01-T1")]
        res = run(rows, FakeBot(allowed={"KXGOODS"}, est={"KXGOODS-26DEC01-T1": 5.0}))
        res["shown"] = np.upcoming_rows(res)
        for e in res["shown"]:
            e["what"] = "a thing & <stuff>"
        return res

    def test_text_and_html_render(self):
        res = self._up()
        text = "\n".join(np._upcoming_text(self._ctx(res)))
        self.assertIn("UPCOMING PROGRAMS", text)
        self.assertIn("expect to quote: 1 series", text)
        self.assertIn("GOODS-26DEC01", text)
        self.assertIn("no (not allowlisted)", text)
        html = np._upcoming_html(self._ctx(res))
        self.assertIn("Upcoming programs", html)
        self.assertIn("a thing &amp; &lt;stuff&gt;", html)     # escaped
        self.assertIn("#1e8449", html)                         # the yes row is green

    def test_a_failed_read_costs_only_the_section(self):
        ctx = self._ctx({"error": "RuntimeError('429')"})
        self.assertIn("unavailable today", np._upcoming_text(ctx)[0])
        self.assertIn("unavailable today", np._upcoming_html(ctx))
        self.assertEqual(np._upcoming_text({"upcoming": None}), [])
        self.assertEqual(np._upcoming_html({"upcoming": None}), "")

    def test_every_quotable_series_is_shown_past_the_top_rows(self):
        rows = [{"kind": "no", "est_total": 10.0 - i} for i in range(5)] + \
               [{"kind": "yes", "est_total": 1.0}, {"kind": "no", "est_total": 0.5},
                {"kind": "maybe", "est_total": 0.1}]
        with mock.patch.object(np, "UPCOMING_ROWS", 3):
            shown = np.upcoming_rows({"rows": rows})
        self.assertEqual([r["kind"] for r in shown], ["no", "no", "no", "yes", "maybe"])

    def test_build_report_survives_a_failed_section(self):
        import test_send_imm_new_programs as t
        t.setUpModule()
        t.clear_files()
        with mock.patch.object(np.imm_upcoming, "build", side_effect=RuntimeError("boom")), \
                mock.patch.object(np, "UPCOMING_SECTION", True):
            rows = [t.prog("KXNEWS-26SEP20-A", t.NOW - timedelta(hours=2),
                           t.NOW + timedelta(days=2))]
            text, html, _s, _e, _seen, _c = t.run_report(rows)
        self.assertIn("UPCOMING PROGRAMS: unavailable today", text)
        self.assertIn("unavailable today", html)


class EnrollReadsUpcoming(unittest.TestCase):
    def test_a_series_only_in_the_upcoming_list_is_enrolled(self):
        import imm_earnings_overrides as ieo
        reads = []

        class _Client:
            def get(self, path, params=None):
                if path == "/incentive_programs":
                    reads.append(params.get("status"))
                    t = ("KXNEWFT-26DEC08-T100" if params.get("status") == "upcoming"
                         else "KXOLDTHING-26DEC08-T1")
                    return {"incentive_programs": [{"market_ticker": t}]}
                if path.startswith("/series/"):
                    return {"series": {}}
                if path.startswith("/markets/"):
                    return {"market": {"rules_secondary": ""}}
                raise AssertionError(path)
        with mock.patch.object(ieo, "log", lambda *a, **k: None), \
                mock.patch.object(imm, "ALLOWLIST_ONLY", True), \
                mock.patch.object(ieo, "classify_series",
                                  lambda s, t: ("enroll", "test") if s == "KXNEWFT"
                                  else ("skip", "")):
            enrolled, _review = ieo.enroll_new_series(_Client(), dry=True)
        self.assertEqual(reads, ["active", "upcoming"])
        self.assertEqual([s for s, _w, _x in enrolled], ["KXNEWFT"])
        reads.clear()
        with mock.patch.object(ieo, "log", lambda *a, **k: None), \
                mock.patch.object(imm, "UPCOMING_ENABLE", False), \
                mock.patch.object(ieo, "classify_series", lambda s, t: ("skip", "")):
            ieo.enroll_new_series(_Client(), dry=True)
        self.assertEqual(reads, ["active"])


if __name__ == "__main__":
    unittest.main()
