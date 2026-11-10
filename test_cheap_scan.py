"""Tests for cheap_scan.py's new-series check (Jack 2026-11-08: "Add it" -- the
daily email flags new far-dated program series for him to approve).

Every path is a temp dir: the tests never touch run-logs/ or the live list.
Run with IMM_STATUS_DIR pointed at a scratch dir, like the other IMM tests.
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import cheap_scan as cs
import incentive_mm as imm


class _TempPaths(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cheapscan_test_")
        self.list_path = os.path.join(self.dir, "cheap_side_books.json")
        self.patches = [
            mock.patch.object(cs, "OUT_DIR", self.dir),
            mock.patch.object(cs, "LOCK", os.path.join(self.dir, "scan.lock")),
            mock.patch.object(cs, "SERIES_FILE", os.path.join(self.dir, "series.json")),
            mock.patch.object(cs, "DISCOVER_OUT", os.path.join(self.dir, "discover.json")),
            mock.patch.object(cs, "DISCOVER_LOCK", os.path.join(self.dir, "discover.lock")),
            mock.patch.object(imm, "cheap_path", lambda: self.list_path),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)


class TestSeriesScope(_TempPaths):
    def test_pat_match(self):
        self.assertEqual(cs.pat_match("KXGRAMMYNOMAOTY", cs.REJECTED_1108), "KXGRAMMYNOM*")
        self.assertEqual(cs.pat_match("KXBOND", cs.REJECTED_1108), "KXBOND")
        self.assertIsNone(cs.pat_match("KXBONDYIELD", cs.REJECTED_1108))   # exact, not a prefix
        self.assertIsNone(cs.pat_match("KXSTATE51", cs.REJECTED_1108))
        self.assertEqual(cs.pat_match("KXPNP", cs.REJECTED_1108), "KXPNP")   # "can be settled tomororw"

    def test_scope_prefixes_approvals_rejections(self):
        st = {"approved": {"KXFOO": {}}, "rejected": {"KXMOONMAN": {}}, "seen": {}}
        prefixes, approved, keep = cs.scope("KXSTATE51,KXMOONMAN", st)
        self.assertEqual(prefixes, ("KXSTATE51", "KXMOONMAN"))
        self.assertTrue(keep("KXSTATE51-29-CA"))
        self.assertTrue(keep("KXFOO-27-A"))
        self.assertFalse(keep("KXFOOBAR-27-A"))        # an approval names a series exactly
        self.assertFalse(keep("KXMOONMAN-31-E"))       # rejected, though a --series prefix starts it
        self.assertFalse(keep("KXBOND-30-TH"))

    def test_the_default_series_is_the_scheduled_list(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(cs.__file__)),
                               "run_cheap_scan_hidden.vbs"), encoding="utf-8") as fh:
            self.assertIn("--series " + cs.TRULY_FAR + " ", fh.read())

    def test_state_missing_corrupt_strict(self):
        st, ok = cs.load_series_state()
        self.assertTrue(ok)
        self.assertEqual(st, {"approved": {}, "rejected": {}, "seen": {}})
        with open(cs.SERIES_FILE, "w") as fh:
            fh.write("{not json")
        st, ok = cs.load_series_state()
        self.assertFalse(ok)
        self.assertEqual(st["approved"], {})
        with self.assertRaises(ValueError):
            cs.load_series_state(strict=True)


class TestDecide(_TempPaths):
    def _args(self, **kw):
        a = dict(series=cs.TRULY_FAR, approve=None, reject=None, why="", write=False)
        a.update(kw)
        return SimpleNamespace(**a)

    def test_approve_then_reject_delists(self):
        self.assertEqual(cs.decide(self._args(approve="kxfoo")), 0)
        st, _ = cs.load_series_state(strict=True)
        self.assertIn("KXFOO", st["approved"])
        with open(self.list_path, "w", encoding="utf-8") as fh:
            json.dump({"markets": {
                "KXFOO-27-A": {"side": "yes", "px": 1, "size": 5000, "stop": "2026-11-15T05:00:00Z"},
                "KXSTATE51-29-CA": {"side": "yes", "px": 1, "size": 5000, "stop": "2026-11-13T05:00:00Z"}}}, fh)
        self.assertEqual(cs.decide(self._args(reject="KXFOO", why="lands in March")), 0)
        st, _ = cs.load_series_state(strict=True)
        self.assertNotIn("KXFOO", st["approved"])
        self.assertEqual(st["rejected"]["KXFOO"]["why"], "lands in March")
        with open(self.list_path, encoding="utf-8") as fh:
            self.assertEqual(sorted((json.load(fh))["markets"]), ["KXSTATE51-29-CA"])
        with open(os.path.join(self.dir, "list_history.jsonl"), encoding="utf-8") as fh:
            hist = [json.loads(x) for x in fh]
        self.assertEqual(sorted(hist[-1]["markets"]), ["KXSTATE51-29-CA"])   # the email's span closes

    def test_a_corrupt_state_is_not_overwritten(self):
        with open(cs.SERIES_FILE, "w") as fh:
            fh.write("{not json")
        self.assertEqual(cs.decide(self._args(approve="KXFOO")), 1)
        with open(cs.SERIES_FILE) as fh:
            self.assertEqual(fh.read(), "{not json")


def _book(yes, no):
    """A /markets/{t}/orderbook reply: yes / no bid levels as (cents, size)."""
    return {"orderbook_fp": {"yes_dollars": [[f"{p / 100:.2f}", f"{n:.2f}"] for p, n in yes],
                             "no_dollars": [[f"{p / 100:.2f}", f"{n:.2f}"] for p, n in no]}}


class TestDiscover(_TempPaths):
    def _args(self, **kw):
        a = dict(series="KXSTATE51", min_days=182.0, max_usd=50.0, bar=1.10)
        a.update(kw)
        return SimpleNamespace(**a)

    def test_discover_writes_the_series_that_would_list(self):
        now = datetime.now(timezone.utc)
        end = now + timedelta(days=4)
        far = (now + timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
        near = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pr = {"pd": 20.0, "start": now - timedelta(days=3), "end": end, "target": 1000.0, "df": 0.5}
        progs = {t: dict(pr) for t in ("KXSTATE51-29-CA", "KXGRAMMYNOMAOTY-69-X", "KXNEWFAR-28-A",
                                       "KXNEWFAR-28-B", "KXNEWFAR-28-C", "KXNEARDATE-26-A",
                                       "KXMIDBOOK-28-A")}

        def mkt(t, exp, yb, ya):
            return {"ticker": t, "event_ticker": t.rsplit("-", 1)[0], "status": "active",
                    "expected_expiration_time": exp, "close_time": exp, "yes_bid_dollars": yb,
                    "yes_ask_dollars": ya, "yes_sub_title": t[-1], "title": f"Will {t[-1]} happen?"}
        markets = {"KXNEWFAR-28-A": mkt("KXNEWFAR-28-A", far, "0.01", "0.03"),
                   "KXNEWFAR-28-B": mkt("KXNEWFAR-28-B", far, "0.01", "0.03"),
                   "KXNEWFAR-28-C": mkt("KXNEWFAR-28-C", far, "0.01", "0.03"),
                   "KXNEARDATE-26-A": mkt("KXNEARDATE-26-A", near, "0.01", "0.03"),
                   "KXMIDBOOK-28-A": mkt("KXMIDBOOK-28-A", far, "0.40", "0.45")}
        # an empty YES side, 5,000 NO bids at 97c (a YES ask at 3c): a lone 1c YES bid holds the side
        books = {t: _book([], [(97, 5000)]) for t in markets}
        read = []

        def rd(path, params=None):
            read.append(path)
            if path == "/markets":
                return {"markets": [markets[t] for t in params["tickers"].split(",") if t in markets]}
            if path.endswith("/orderbook"):
                return books.get(path.split("/")[2])
            if path.startswith("/series/"):
                return {"series": {"title": "New far thing", "category": "Politics"}}
            return None
        asked = []

        def claude(prompt):
            asked.append(prompt)
            return '{"KXNEWFAR": {"timing": "scheduled", "why": "decided by the 2028 vote"}}', 0.03, ""
        with mock.patch.object(cs, "programs", lambda client, keep, now: progs), \
                mock.patch.object(cs, "rd", rd), \
                mock.patch.object(cs, "own_orders", lambda client: ({}, {}, {"KXNEWFAR-28-C"})), \
                mock.patch.object(imm, "_fair_reader_client", lambda: None), \
                mock.patch.object(imm.IncentiveMarketMaker, "_blocked", staticmethod(lambda t: False)), \
                mock.patch.object(cs, "run_claude", claude), \
                mock.patch.object(cs.time, "sleep", lambda s: None):
            self.assertEqual(cs.discover(self._args()), 0)
        self.assertEqual(len(asked), 1)
        self.assertIn('"series": "KXNEWFAR"', asked[0])
        self.assertIn("Will A happen?", asked[0])                # the market titles go in
        with open(cs.DISCOVER_OUT, encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertEqual(d["outside"], 5)                       # KXSTATE51 in scope, the Grammy noms rejected
        self.assertEqual(d["rejected_markets"], 1)
        self.assertEqual(d["far_near_best"], 3)                 # 30 days out, and a 40x45 book, never read
        self.assertNotIn("/markets/KXMIDBOOK-28-A/orderbook", read)
        self.assertEqual([c["series"] for c in d["candidates"]], ["KXNEWFAR"])
        c = d["candidates"][0]
        self.assertTrue(c["new"])
        self.assertEqual(c["n_list"], 2)
        self.assertEqual(c["yours"], 1)                         # your orders on C: the standoff
        self.assertEqual(c["category"], "Politics")
        self.assertEqual(c["top"][0]["side"], "yes")
        self.assertEqual((c["timing"], c["timing_why"]), ("scheduled", "decided by the 2028 vote"))
        st, _ = cs.load_series_state(strict=True)
        self.assertIn("KXNEWFAR", st["seen"])
        self.assertEqual(st["seen"]["KXNEWFAR"]["timing"]["label"], "scheduled")

        # one email shows it, then it waits; the timing call is reused, not re-asked
        cs.mark_emailed(["KXNEWFAR"], "2026-11-09")
        with mock.patch.object(cs, "programs", lambda client, keep, now: progs), \
                mock.patch.object(cs, "rd", rd), \
                mock.patch.object(cs, "own_orders", lambda client: ({}, {}, set())), \
                mock.patch.object(imm, "_fair_reader_client", lambda: None), \
                mock.patch.object(imm.IncentiveMarketMaker, "_blocked", staticmethod(lambda t: False)), \
                mock.patch.object(cs, "run_claude", claude), \
                mock.patch.object(cs.time, "sleep", lambda s: None):
            cs.discover(self._args())
        with open(cs.DISCOVER_OUT, encoding="utf-8") as fh:
            c = json.load(fh)["candidates"][0]
        self.assertFalse(c["new"])
        self.assertEqual(c["n_list"], 3)
        self.assertEqual(len(asked), 1)
        self.assertEqual(c["timing"], "scheduled")


class TestTimingCheck(_TempPaths):
    def _cands(self):
        return [{"series": s, "title": f"{s} title", "category": "Science", "horizon_min": "2028-01-07T15:00:00Z",
                 "usd_day": u, "top": [{"title": f"{s}?", "sub": "x", "rules": "If ...", "early": ""}]}
                for s, u in (("KXPROOF", 0.5), ("KXVOTE", 2.0))]

    def test_parse_and_a_failed_run_leaves_them_unchecked(self):
        st = {"approved": {}, "rejected": {}, "seen": {}}
        now = datetime(2026, 11, 9, 13, 30, tzinfo=timezone.utc)
        cands = self._cands()
        reply = ('Sure:\n{"KXPROOF": {"timing": "Sudden", "why": "a proof can land any day"},'
                 ' "KXVOTE": {"timing": "scheduled", "why": "the 2028 vote"}, "KXOTHER": {"timing": "slow"}}')
        with mock.patch.object(cs, "run_claude", lambda p: (reply, 0.02, "")):
            self.assertAlmostEqual(cs.timing_check(cands, st, now), 0.02)
        self.assertEqual([(c["timing"], c["timing_why"]) for c in cands],
                         [("sudden", "a proof can land any day"), ("scheduled", "the 2028 vote")])
        cands = self._cands()
        with mock.patch.object(cs, "run_claude", lambda p: ("", 0.0, "exit 1: Failed to authenticate")):
            cs.timing_check(cands, st, now)
        self.assertTrue(all("timing" not in c and "authenticate" in c["timing_err"] for c in cands))
        cands = self._cands()
        with mock.patch.object(cs, "run_claude", lambda p: ("no json here", 0.01, "")):
            cs.timing_check(cands, st, now)
        self.assertTrue(all("unreadable reply" in c["timing_err"] for c in cands))
        cands = self._cands()
        with mock.patch.object(cs, "TIMING_MODE", "off"), \
                mock.patch.object(cs, "run_claude", lambda p: self.fail("asked with the check off")):
            cs.timing_check(cands, st, now)
        self.assertTrue(all("off" in c["timing_err"] for c in cands))

    def test_a_cached_call_covers_its_events_only(self):
        now = datetime(2026, 11, 9, 13, 30, tzinfo=timezone.utc)
        st = {"approved": {}, "rejected": {}, "seen": {"KXVOTE": {"timing": {
            "label": "scheduled", "why": "2028 vote", "at": "2026-11-01T13:30:00Z", "events": ["KXVOTE-28"]}}}}
        asked = []
        c = dict(self._cands()[1], events=["KXVOTE-28"])
        with mock.patch.object(cs, "run_claude", lambda p: (asked.append(p) or "{}", 0.0, "")):
            cs.timing_check([c], st, now)
        self.assertEqual((asked, c["timing"]), ([], "scheduled"))
        c = dict(self._cands()[1], events=["KXVOTE-26", "KXVOTE-28"])    # a new event: asked again
        with mock.patch.object(cs, "run_claude", lambda p: (asked.append(p) or
                                                            '{"KXVOTE": {"timing": "soon", "why": "2026"}}', 0.0, "")):
            cs.timing_check([c], st, now)
        self.assertEqual((len(asked), c["timing"]), (1, "soon"))

    def test_the_prompt_carries_the_desks_calls_and_the_cutoff(self):
        seen = []
        now = datetime(2026, 11, 9, 13, 30, tzinfo=timezone.utc)
        with mock.patch.object(cs, "run_claude", lambda p: (seen.append(p) or "{}", 0.0, "")):
            cs.timing_check(self._cands(), {"approved": {}, "rejected": {}, "seen": {}}, now)
        self.assertIn("2027-05-10", seen[0])                     # today + 182 days
        self.assertIn("KXPNP", seen[0])
        self.assertIn('"series": "KXVOTE"', seen[0])


class TestRenderDiscover(unittest.TestCase):
    def _c(self, s, new, usd=3.0, timing="scheduled"):
        return {"series": s, "title": f"{s} title", "category": "Politics", "first_seen": "2026-11-09T13:30:00Z",
                "timing": timing, "timing_why": f"{s} why",
                "new": new, "n_list": 2, "yours": 0, "bot_more": 0, "usd_day": usd, "risk": 100.0, "proj": 9.0,
                "horizon_min": "2028-11-07T15:00:00Z", "horizon_max": "2028-11-07T15:00:00Z",
                "program_end": "2026-11-15T05:00:00Z", "early": "This market will close early if the event occurs.",
                "top": [{"ticker": f"{s}-28-A", "sub": "A", "side": "yes", "px": 1, "size": 5000,
                         "share": 0.25, "usd_day": 2.0}]}

    def test_missing_none_new_waiting_stale(self):
        now = datetime(2026, 11, 9, 13, 40, tzinfo=timezone.utc)
        lines, n = cs.render_discover(None, now)
        self.assertEqual(n, 0)
        self.assertIn("did not run", lines[-1])
        d = {"at": "2026-11-09T13:30:00Z", "books": 300, "watching": ["A", "B"], "candidates": []}
        lines, n = cs.render_discover(d, now)
        self.assertIn("none:", lines[-1])
        d["candidates"] = [self._c("KXNEW", True), self._c("KXOLD", False)]
        lines, n = cs.render_discover(d, now)
        text = "\n".join(lines)
        self.assertEqual(n, 1)
        self.assertIn("NEW KXNEW -- KXNEW title [Politics]", text)
        self.assertIn("KXNEW-28-A (A): YES bid 1c x5,000, 25% of its side", text)
        self.assertIn("waiting since 2026-11-09: KXOLD", text)
        self.assertIn("approve <series>", text)
        self.assertIn("Could it settle tomorrow", text)
        self.assertIn('Kalshi on an early close: "This market will close early if the event occurs."', text)
        self.assertNotIn("STALE", text)
        self.assertIn("Timing check: SCHEDULED -- KXNEW why", text)
        lines, _ = cs.render_discover(d, now + timedelta(days=2))
        self.assertIn("STALE", lines[1])

    def test_left_out_and_unchecked(self):
        now = datetime(2026, 11, 9, 13, 40, tzinfo=timezone.utc)
        unchecked = self._c("KXUNK", True, timing=None)
        unchecked["timing_err"] = "exit 1: Failed to authenticate"
        d = {"at": "2026-11-09T13:30:00Z", "books": 300, "watching": ["A"],
             "candidates": [self._c("KXPROOF", True, timing="sudden"), self._c("KXOLDSUD", False, timing="soon"),
                            unchecked]}
        lines, n = cs.render_discover(d, now)
        text = "\n".join(lines)
        self.assertEqual(n, 1)                                  # the unchecked one; the left-out is not "new"
        self.assertIn("1 new, 0 still waiting, 1 left out", text)
        self.assertIn("Left out by the timing check", text)
        self.assertIn("KXPROOF -- KXPROOF title (sudden: KXPROOF why", text)
        self.assertNotIn("KXOLDSUD", text)                      # named once, already emailed
        self.assertIn("Timing check: NOT CHECKED (exit 1: Failed to authenticate) -- judge it yourself", text)
        d["candidates"] = [self._c("KXPROOF", True, timing="sudden")]
        lines, n = cs.render_discover(d, now)
        self.assertEqual(n, 0)
        self.assertIn("and that the timing check passes", "\n".join(lines))


class TestDollarFloor(_TempPaths):
    """Jack 2026-11-09: "Build it" -- the $1 floor in the hourly scan: a listed
    market projecting under ~$1.20 for its period has its rung raised toward
    $200; when even $200 can't reach $1 it comes off the list."""
    HRS = 39.0                                  # 11/09 13:51Z -> the period's end 11/11 05:00Z

    @staticmethod
    def _c(size, usd_day, px=1, side="yes"):
        return {"side": side, "px": px, "size": size, "share": 0.0, "usd_day": usd_day,
                "risk": size * px / 100.0, "proj": usd_day * TestDollarFloor.HRS / 24.0}

    def _cands(self, per10k):
        """A 1c ladder whose $/day grows with size (10,000 earns `per10k`)."""
        return [self._c(n, per10k * n / 10000.0) for n in (5000, 10000, 15000, 20000)]

    def test_pick_rung_stays_within_the_list_cap(self):
        cands = self._cands(0.40)
        self.assertEqual(cs.pick_rung(cands, 100.0)["size"], 10000)
        self.assertEqual(cs.pick_rung(cands, 200.0)["size"], 20000)
        self.assertIsNone(cs.pick_rung([self._c(20000, 1.0)], 100.0))

    def test_tops_up_to_the_smallest_rung_that_reaches_the_aim(self):
        # E45 at 13:51Z: banked $0.35, 10,000 at 1c earns ~$0.29/day -> $0.82 for the period
        # 15,000 projects 0.35 + 0.71 = $1.06, 20,000 projects 0.35 + 0.94 = $1.29: the aim
        rung, act = cs.floor_rung(self._cands(0.29), 0.35, self.HRS, 1.20, 1.00)
        self.assertEqual((rung["size"], act), (20000, "aim"))

    def test_actions(self):
        cands = self._cands(0.40)                   # 10,000: $0.65 over 39h; 20,000: $1.30
        rung, act = cs.floor_rung(cands, 0.30, self.HRS, 1.20, 1.00)
        self.assertEqual((rung["size"], act), (15000, "aim"))     # 0.30 + 0.975 = 1.275
        rung, act = cs.floor_rung(cands, 0.0, self.HRS, 1.40, 1.00)
        self.assertEqual((rung["size"], act), (20000, "short"))   # 1.30: short of 1.40, over $1
        rung, act = cs.floor_rung(self._cands(0.08), 0.03, self.HRS, 1.20, 1.00)
        self.assertEqual((rung, act), (None, "drop"))             # PRIMEENG-like: $0.29 at best
        # unknown banked (the bot's state is on another period): never dropped blind
        rung, act = cs.floor_rung(self._cands(0.08), None, self.HRS, 1.20, 1.00)
        self.assertEqual((rung["size"], act), (20000, "short"))
        self.assertEqual(cs.floor_rung([], 0.5, self.HRS, 1.20, 1.00), (None, "drop"))

    def test_period_banked_from_the_bots_state(self):
        p = os.path.join(self.dir, "imm_state.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"accrued_est": {"A-1": 0.8435, "B-1": 2.43, "C-1": 1.0},
                       "period_base": {"A-1": 0.4945},
                       "period_start": {"A-1": "2026-11-04T05:00:00+00:00",
                                        "B-1": "2026-11-06T14:25:26+00:00"}}, fh)
        bk = cs.period_banked(p)
        start = datetime(2026, 11, 4, 5, tzinfo=timezone.utc)
        self.assertAlmostEqual(cs.banked_for(bk, "A-1", start), 0.349, places=3)
        self.assertAlmostEqual(cs.banked_for(bk, "B-1", datetime(2026, 11, 6, 14, 25, 26,
                                                                 tzinfo=timezone.utc)), 2.43)
        self.assertIsNone(cs.banked_for(bk, "B-1", start))          # the state is on another period
        self.assertIsNone(cs.banked_for(bk, "C-1", start))          # no period start in the state
        self.assertIsNone(cs.banked_for(bk, "D-1", start))
        self.assertEqual(cs.period_banked(os.path.join(self.dir, "missing.json")), {})

    def test_apply_floor_moves_the_row(self):
        a = SimpleNamespace(floor_aim=1.20, floor=1.00, floor_max_usd=200.0)
        why = {"floor top-up": 0, "floor drop": 0}
        cands = self._cands(0.40)
        pick = cs.pick_rung(cands, 100.0)
        r = {"verdict": "LIST", "pick": pick}
        cs.apply_floor(r, "KXA-1", pick, cands, 0.30, self.HRS, a, why)
        self.assertEqual((r["verdict"], r["pick"]["size"]), ("LIST", 15000))
        self.assertAlmostEqual(r["proj_total"], 1.275, places=2)     # 0.30 + 0.975
        self.assertEqual(r["floor"]["action"], "aim")
        self.assertEqual(why["floor top-up"], 1)
        # banked enough already: untouched
        r = {"verdict": "LIST", "pick": pick}
        cs.apply_floor(r, "KXA-1", pick, cands, 0.90, self.HRS, a, why)
        self.assertEqual((r["pick"]["size"], r.get("floor")), (10000, None))
        # even $200 can't reach $1: off the list
        cands = self._cands(0.08)
        pick = cs.pick_rung(cands, 100.0)
        r = {"verdict": "LIST", "pick": pick}
        cs.apply_floor(r, "KXB-1", pick, cands, 0.03, self.HRS, a, why)
        self.assertEqual((r["verdict"], r["floor"]["action"]), ("floor drop", "drop"))
        self.assertEqual(why["floor drop"], 1)
        with open(os.path.join(self.dir, "scan.log"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("floor: KXA-1 banked $0.30", text)
        self.assertIn("-> YES 1c x15000 ($150)", text)
        self.assertIn("KXB-1", text)
        self.assertIn("OFF the list", text)


class TestHeldKeep(_TempPaths):
    """Jack 2026-11-10: "if i already have $100 of exposure, stop quoting that
    side. and try to unwind at breakeven or better" -- the bot unwinds only a
    listed market, so one it holds $100+ on stays listed while held."""

    def test_held_positions_and_keep(self):
        p = os.path.join(self.dir, "imm_state.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"own_pos": {"G-1": 10000.0, "S-1": 120.0, "N-1": -5000.0, "B-1": "x"},
                       "own_avg": {"G-1": 1.0, "S-1": 0.4, "N-1": 98.0}}, fh)
        held = cs.held_positions(p)
        self.assertEqual(held["G-1"], (10000.0, 1.0))
        self.assertEqual(held["N-1"], (-5000.0, 98.0))
        self.assertNotIn("B-1", held)
        self.assertEqual(cs.held_positions(os.path.join(self.dir, "missing.json")), {})
        now = datetime(2026, 11, 12, 4, 0, tzinfo=timezone.utc)
        listed = {"G-1": {"side": "yes", "px": 0.6, "size": 1667, "stop": "2026-11-12T05:00:00Z"},
                  "S-1": {"side": "yes", "px": 0.4, "size": 18750, "stop": "2026-11-12T05:00:00Z"},
                  "N-1": {"side": "no", "px": 2, "size": 5000, "stop": "2026-11-20T05:00:00Z"},
                  "T-1": {"side": "yes", "px": 1, "size": 10000, "stop": "2026-11-12T05:00:00Z"}}
        out = cs.held_keep(listed, {"T-1"}, lambda t: True, held, now)
        self.assertEqual(set(out), {"G-1", "N-1"})                 # $100 / $100; S-1 is $0.48
        self.assertEqual(out["G-1"]["stop"], "2026-11-13T16:00:00Z")   # 1h left: rolled 36h
        self.assertEqual(out["N-1"]["stop"], "2026-11-20T05:00:00Z")   # far enough: kept as is
        self.assertIn("$100 stop", out["G-1"]["held"])
        self.assertEqual(listed["G-1"]["stop"], "2026-11-12T05:00:00Z")  # the input untouched
        self.assertEqual(cs.held_keep(listed, set(), lambda t: t != "G-1", held, now).keys(), {"N-1"})
        with mock.patch.object(imm, "CHEAP_STOP_USD", 0.0):
            self.assertEqual(cs.held_keep(listed, set(), lambda t: True, held, now), {})


class TestSubPenny(unittest.TestCase):
    """Jack 2026-11-10: "it's ok to go lower to 0.1% to catch markets like
    KXFRENCHPRES-27" -- on a market priced in tenths of a cent the scan also
    tries 0.1-0.9c rungs, sized in dollars."""
    PR = {"pd": 14.29, "target": 1000.0, "df": 0.5}

    @staticmethod
    def _book(yes, no):
        return {"orderbook_fp": {"yes_dollars": [[f"{p / 100:.4f}", f"{n:.2f}"] for p, n in yes],
                                 "no_dollars": [[f"{p / 100:.4f}", f"{n:.2f}"] for p, n in no]}}

    def test_prices_tried(self):
        whole = cs.rung_prices([[1.0, 300]], [[97.0, 1500]])
        self.assertEqual([p for p, _ in whole], [1, 2])
        self.assertEqual([p for p, _ in cs.rung_prices([[1.0, 300]], [[97.0, 1500]], step=0.001)],
                         [1, 2] + [k / 10 for k in range(1, 10)])
        sub = dict(cs.rung_prices([[0.2, 4]], [[99.7, 5000]]))     # read off the book
        self.assertEqual(sub[0.2][:3], (5000, 7500, 10000))         # $10, $15, $20 at 0.2c
        self.assertEqual(sub[0.1][-1], 200000)                      # $200 at 0.1c
        self.assertEqual(len(cs.rung_prices([[0.2, 4]], [[99.7, 5000]], step=0.01)), 2)
        with mock.patch.object(imm, "CHEAP_PX_MIN", 1.0):
            self.assertEqual(len(cs.rung_prices([[0.2, 4]], [[99.7, 5000]])), 2)

    def test_picks_the_subpenny_rung(self):
        # KXFRENCHPRES-27-MPAN at 11/10 02:50Z: YES 0.2x0.3, 11,112 at 0.1c, 4 at 0.2c
        ob = self._book([(0.1, 11112), (0.2, 4)], [(99.7, 5000)])
        cands = cs.rung_cands("KXFRENCHPRES-27-MPAN", self.PR, ob, [], [], 100.0, 48.0, step=0.001)
        self.assertTrue(cands)
        self.assertFalse([c for c in cands if c["px"] >= 1])        # a 1c bid would cross
        self.assertEqual({c["px"] for c in cands}, {0.1, 0.2})      # 0.3c+ steps down to 0.2c
        pick = cs.pick_rung(cands, 100.0)
        self.assertEqual((pick["side"], pick["px"], pick["size"]), ("yes", 0.2, 5000))
        self.assertAlmostEqual(pick["risk"], 10.0)
        self.assertAlmostEqual(pick["usd_day"], 5000 / 5004 / 2 * 14.29, places=3)
        self.assertEqual(cs._rung_s(pick), "YES 0.2c x5000 ($10)")
        # the same book on a whole-cent market: nothing
        self.assertEqual(cs.rung_cands("X-1", self.PR, ob, [], [], 100.0, 48.0, step=0.01), [])


if __name__ == "__main__":
    unittest.main()
