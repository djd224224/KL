"""wave_detect's KNOWN SHAPES rule (Jack 2026-11-14: "for upcoming new shapes, notify me and it's
my call so dont default to turning it on e.g. KXYTVIEWSHIGH. but default to running the wave for
elections, after economic events, and things of similar shapes to what's been run on waves so
far"), the live-feed screen and the registry's LISTED rule. No network: kalshi_reads is a
stand-in, and the bot's own live-source screen decides what a live feed is."""
import argparse
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

if "kalshi_reads" not in sys.modules:
    _m = types.ModuleType("kalshi_reads")
    _m.kalshi_get = lambda *a, **k: {}
    _m.kalshi_get_all = lambda *a, **k: []
    sys.modules["kalshi_reads"] = _m
import wave_detect as wd  # noqa: E402

NOW = datetime(2026, 11, 14, 18, 0, tzinfo=timezone.utc)
YT = [{"name": "YouTube", "url": "https://charts.youtube.com/US"}]
NEWS = [{"name": "AP", "url": "https://apnews.com"}]


def z(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


class Args(argparse.Namespace):
    def __init__(self, **kw):
        super().__init__(min_markets=40, min_pool=1000.0, min_days_out=14.0, min_soon_days=1.0, min_events=5)
        for k, v in kw.items():
            setattr(self, k, v)


def rows(s, n_events=6, per=8, days=60.0, usd=25.0):
    """(g, ms) for judge(): n_events x per markets settling `days` out."""
    g = {"markets": {}, "pool": 0.0, "start": NOW, "end": NOW + timedelta(days=1)}
    ms = {}
    for e in range(n_events):
        for k in range(per):
            t = f"{s}-E{e}-M{k}"
            g["markets"][t] = {"pool": usd, "start": NOW, "end": NOW + timedelta(days=1), "target": 1000.0}
            g["pool"] += usd
            ms[t] = {"ticker": t, "event_ticker": f"{s}-E{e}", "title": f"{s} E{e}",
                     "close_time": z(NOW + timedelta(days=days)), "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.46"}
    return g, ms


class ShapeTests(unittest.TestCase):
    def setUp(self):
        self.saved = (wd.imm.IncentiveMarketMaker.__dict__["_allowed"], wd.imm.IncentiveMarketMaker.__dict__["_blocked"])
        wd.imm.IncentiveMarketMaker._allowed = classmethod(lambda cls, t: False)
        wd.imm.IncentiveMarketMaker._blocked = staticmethod(lambda t: False)
        self.ctx = {"listed": set(), "quoted": set(), "reg_series": wd.registry_series({"waves": {
            "w-fx": {"series": ["KXGOLDEVENT"], "enabled": True, "window": "fx-shut"},
            "w-capped": {"series": ["KXBTCEVENT"], "enabled": True, "stop_cap": "2026-11-15T05:00:00Z"},
            "w-off": {"series": ["KXTHEVOICE"], "enabled": False},
            "w-elec": {"series": ["KXSENATENOM"]}}})}

    def tearDown(self):
        wd.imm.IncentiveMarketMaker._allowed, wd.imm.IncentiveMarketMaker._blocked = self.saved

    def judge(self, s, meta, at=NOW, **kw):
        g, ms = rows(s, **kw)
        return wd.judge(s, g, ms, meta, self.ctx, Args(), at)

    def test_elections_and_politics_are_known_shapes(self):
        self.assertEqual(self.judge("KXMAYORRACE", {"category": "Elections", "settlement_sources": NEWS})["verdict"],
                         "FIT")
        r = self.judge("KXBILLPASS", {"category": "Politics", "title": "Will the bill pass"}, days=6)
        self.assertEqual((r["verdict"], r["dated"]), ("NEAR", "NEAR"))

    def test_kxytviewshigh_is_a_new_shape(self):
        r = self.judge("KXYTVIEWSHIGH", {"category": "Entertainment", "tags": ["Views", "Music", "Monthly Views"],
                                         "title": "highest daily views in time period", "settlement_sources": YT},
                       n_events=16, per=9, days=18)
        self.assertEqual((r["verdict"], r["dated"]), ("NEW", "FIT"))
        self.assertIn("new shape: Entertainment (Views, Music, Monthly Views) -- no wave has run one", r["why"])
        self.assertTrue(any(w.startswith("settles on a live feed (charts.youtube.com)") for w in r["why"]))

    def test_a_new_shape_dated_near_keeps_its_dated_verdict(self):
        r = self.judge("KXSCIX", {"category": "Science and Technology", "title": "Which lab"}, days=5)
        self.assertEqual((r["verdict"], r["dated"]), ("NEW", "NEAR"))

    def test_no_category_read_fails_closed(self):
        r = self.judge("KXWHAT", {})
        self.assertEqual(r["verdict"], "NEW")
        self.assertIn("new shape: no category read -- no wave has run one", r["why"])

    def test_screens_still_come_first(self):
        self.assertEqual(self.judge("KXGAME", {"category": "Sports"})["verdict"], "CHECK")
        r = self.judge("KXVOICEX", {"category": "Entertainment", "title": "Who wins The Voice"})
        self.assertEqual(r["verdict"], "CHECK")                 # info risk: pre-taped TV
        self.assertEqual(self.judge("KXFAST", {"category": "Entertainment"}, days=0.5)["verdict"], "SOON")
        self.assertEqual(self.judge("KXONE", {"category": "Entertainment"}, n_events=2)["verdict"], "FEW")

    def test_a_known_shape_on_a_live_feed_is_check(self):
        r = self.judge("KXPOLLAVG", {"category": "Politics", "title": "Approval average",
                                     "settlement_sources": [{"name": "RCP", "url": "https://www.realclearpolling.com/x"}]})
        self.assertEqual(r["verdict"], "CHECK")
        self.assertTrue(any("live feed (www.realclearpolling.com)" in w for w in r["why"]))

    def test_the_after_economic_events_waves_relight_their_own_series(self):
        r = self.judge("KXGOLDEVENT", {"category": "Commodities"}, days=3)
        self.assertEqual((r["verdict"], r["registry_wave"], r["relight"]), ("LISTED", "w-fx", True))
        self.assertTrue(any("in the FX close only" in w for w in r["why"]))

    def test_a_capped_wave_past_its_cap_lists_nothing(self):
        before = self.judge("KXBTCEVENT", {"category": "Crypto"}, days=3)
        self.assertEqual(before["verdict"], "LISTED")
        after = self.judge("KXBTCEVENT", {"category": "Crypto"}, at=datetime(2026, 11, 20, tzinfo=timezone.utc), days=3)
        self.assertEqual(after["verdict"], "CHECK")
        self.assertTrue(any("live-price ladder" in w for w in after["why"]))

    def test_a_ladder_no_wave_lists_is_check(self):
        r = self.judge("KX10YRRATEEVENT", {"category": "Financials"}, days=3)
        self.assertEqual(r["verdict"], "CHECK")
        self.assertTrue(any("no registry wave lists" in w for w in r["why"]))

    def test_a_wave_jack_turned_off_no_longer_reads_listed(self):
        r = self.judge("KXTHEVOICE", {"category": "Entertainment", "title": "WHO WILL WIN THE VOICE?"})
        self.assertEqual(r["verdict"], "CHECK")
        self.assertEqual(wd.registry_wave(self.ctx, "KXTHEVOICE", NOW), "")

    def test_a_listed_registry_series_is_a_renewal_not_a_relight(self):
        g, ms = rows("KXSENATENOM")
        self.ctx["listed"] = set(g["markets"])
        r = wd.judge("KXSENATENOM", g, ms, {"category": "Elections"}, self.ctx, Args(), NOW)
        self.assertEqual((r["verdict"], r["relight"]), ("LISTED", False))

    def test_wave_call_holds_new_shapes_for_jack(self):
        known = self.judge("KXMAYORRACE", {"category": "Elections"}, n_events=2, per=4)     # FEW
        new = self.judge("KXYTVIEWSHIGH", {"category": "Entertainment"}, n_events=16, per=9)
        c = wd.wave_call([known, new], {}, Args())
        self.assertFalse(c["wave"])
        self.assertTrue(c["needs_jack"])
        self.assertEqual([r["series"] for r in c["new_shape"]], ["KXYTVIEWSHIGH"])
        self.assertTrue(c["new_shape_over_bar"])
        c = wd.wave_call([new], {"KXYTVIEWSHIGH": "2026-11-08T23:08:00Z"}, Args())   # alerted before
        self.assertFalse(c["needs_jack"] or c["new_shape_over_bar"])


if __name__ == "__main__":
    unittest.main()
