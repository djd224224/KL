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
        with mock.patch.object(cs, "programs", lambda client, keep, now: progs), \
                mock.patch.object(cs, "rd", rd), \
                mock.patch.object(cs, "own_orders", lambda client: ({}, {}, {"KXNEWFAR-28-C"})), \
                mock.patch.object(imm, "_fair_reader_client", lambda: None), \
                mock.patch.object(imm.IncentiveMarketMaker, "_blocked", staticmethod(lambda t: False)), \
                mock.patch.object(cs.time, "sleep", lambda s: None):
            self.assertEqual(cs.discover(self._args()), 0)
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
        st, _ = cs.load_series_state(strict=True)
        self.assertIn("KXNEWFAR", st["seen"])

        # one email shows it, then it waits
        cs.mark_emailed(["KXNEWFAR"], "2026-11-09")
        with mock.patch.object(cs, "programs", lambda client, keep, now: progs), \
                mock.patch.object(cs, "rd", rd), \
                mock.patch.object(cs, "own_orders", lambda client: ({}, {}, set())), \
                mock.patch.object(imm, "_fair_reader_client", lambda: None), \
                mock.patch.object(imm.IncentiveMarketMaker, "_blocked", staticmethod(lambda t: False)), \
                mock.patch.object(cs.time, "sleep", lambda s: None):
            cs.discover(self._args())
        with open(cs.DISCOVER_OUT, encoding="utf-8") as fh:
            c = json.load(fh)["candidates"][0]
        self.assertFalse(c["new"])
        self.assertEqual(c["n_list"], 3)


class TestRenderDiscover(unittest.TestCase):
    def _c(self, s, new, usd=3.0):
        return {"series": s, "title": f"{s} title", "category": "Politics", "first_seen": "2026-11-09T13:30:00Z",
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
        lines, _ = cs.render_discover(d, now + timedelta(days=2))
        self.assertIn("STALE", lines[1])


if __name__ == "__main__":
    unittest.main()
