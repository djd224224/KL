"""Tests for trump_mention_fair.py (KXTRUMPMENTION / KXTRUMPMENTIONB pre-event
fairs; Jack 2026-10-25: "weight near-term more heavily"). No network: Kalshi
is a fixture behind get_text / get_json, and requests.get fails loudly."""
import json
import math
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import urlparse

import trump_mention_fair as tmf

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
RALLY_RULES = "If Donald Trump says {w} as part of Midterm Rally in Waco, Texas originally " \
              "scheduled for October 27, 2026, then the market resolves to Yes."
_n = [0]


def mk(event, word, result="", status="finalized", close=None, settle=None,
       title="What will Donald Trump say during Midterm Rally in Waco, Texas?", rules=None,
       bid=None, ask=None):
    """A Kalshi market row as /markets and /events return it."""
    _n[0] += 1
    return {"ticker": f"{event}-W{_n[0]}", "event_ticker": event, "custom_strike": {"Word": word},
            "yes_sub_title": word, "result": result, "status": status,
            "close_time": close, "settlement_ts": settle, "title": title,
            "rules_primary": (rules or RALLY_RULES).format(w=word),
            "yes_bid_dollars": bid, "yes_ask_dollars": ask}


class FakeKalshi:
    """get_text(url, params) -> JSON text for /markets, /historical/markets
    and /events; every call recorded; `fail` paths raise; `page` splits
    lists into cursor pages."""

    def __init__(self, settled=(), archive=(), open_events=(), page=0):
        self.settled, self.archive, self.open_events = list(settled), list(archive), list(open_events)
        self.calls, self.fail, self.page = [], set(), page

    def _pages(self, rows, params, key):
        if not self.page:
            return {key: rows, "cursor": ""}
        i = int(params.get("cursor") or 0)
        nxt = i + self.page
        return {key: rows[i:nxt], "cursor": str(nxt) if nxt < len(rows) else ""}

    def __call__(self, url, params):
        path = urlparse(url).path.replace("/trade-api/v2", "")
        self.calls.append((path, dict(params)))
        if path in self.fail:
            raise OSError(f"{path} down")
        s = params.get("series_ticker")
        if path == "/markets":
            rows = [m for m in self.settled if m["event_ticker"].split("-")[0] == s]
            if "min_settled_ts" in params:
                rows = [m for m in rows if (tmf._epoch(m.get("settlement_ts")) or 0)
                        >= params["min_settled_ts"]]
            return json.dumps(self._pages(rows, params, "markets"))
        if path == "/historical/markets":
            rows = [m for m in self.archive if m["event_ticker"].split("-")[0] == s]
            return json.dumps(self._pages(rows, params, "markets"))
        if path == "/events":
            rows = [e for e in self.open_events if e["event_ticker"].split("-")[0] == s]
            return json.dumps(self._pages(rows, params, "events"))
        raise AssertionError(f"unexpected path {path}")

    def paths(self):
        return [p for p, _ in self.calls]


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def settled_event(event, words, day, rules=None, title=None):
    """Markets of one settled event: words {word: 'yes'/'no'}, closed and
    settled on `day` (a datetime)."""
    kw = {}
    if rules:
        kw["rules"] = rules
    if title:
        kw["title"] = title
    return [mk(event, w, r, close=iso(day + timedelta(hours=4)),
               settle=iso(day + timedelta(hours=4, minutes=30)), **kw)
            for w, r in words.items()]


def open_event(event, words, status="active"):
    return {"event_ticker": event, "title": "What will Trump say during his rally in Waco?",
            "sub_title": "Donald Trump - midterm rally in Waco, Texas",
            "markets": [mk(event, w, status=status, bid="0.6000", ask="0.6500") for w in words]}


class Base(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "trump_mention_fair.json")
        p = mock.patch.object(tmf.requests, "get", side_effect=AssertionError("network"))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(tmf, "SEED_FILE", os.path.join(self.dir, "no_seed.json"))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(tmf, "_log")
        self.log = p.start()
        self.addCleanup(p.stop)
        for name, v in (("HALF_LIFE_WORD_TYPE_DAYS", 30.0), ("HALF_LIFE_DAYS", 60.0),
                        ("WORD_TYPE_PRIOR_N", 3.0), ("WORD_PRIOR_N", 5.0),
                        ("TYPE_PRIOR_N", 20.0), ("DUD_PRIOR_N", 10.0), ("MIN_HISTORY_N", 0.0)):
            p = mock.patch.object(tmf, name, v)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(shutil.rmtree, self.dir, True)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)


class TestWords(unittest.TestCase):

    def test_norm_word(self):
        self.assertEqual(tmf.norm_word("Oil / Gas / Gasoline"), "gas / gasoline / oil")
        self.assertEqual(tmf.norm_word("Gas/Gasoline /  OIL "), "gas / gasoline / oil")
        self.assertEqual(tmf.norm_word("Iran (5+ times)"), "iran (5+ times)")
        self.assertNotEqual(tmf.norm_word("Iran (5+ times)"), tmf.norm_word("Iran"))
        self.assertEqual(tmf.norm_word("Xi Jinping’s"), "xi jinping's")
        self.assertEqual(tmf.norm_word("Event does not qualify"), tmf.EDQ_KEY)
        self.assertEqual(tmf.norm_word(""), "")

    def test_event_type(self):
        def typ(title, name):
            return tmf.event_type(tmf.market_event_text(
                mk("KXTRUMPMENTION-26OCT20", "Biden", title=title,
                   rules="If Donald Trump says {w} as part of " + name + ", then the market resolves to Yes.")))
        self.assertEqual(typ("What will Donald Trump say during Midterm Rally in Waco?",
                             "Midterm Rally in Waco, Texas"), "rally")
        self.assertEqual(typ("What will Donald Trump say during Tele-Rally with Andy Ogles?",
                             "Tele-Rally with Andy Ogles"), "telerally")
        self.assertEqual(typ("What will Donald Trump say during Fox News: Hannity?",
                             "Fox News: Hannity"), "interview")
        self.assertEqual(typ("What will Donald Trump say during THE PRESIDENT meets with the "
                             "Prime Minister of Japan?", "THE PRESIDENT meets with the Prime "
                             "Minister of Japan"), "bilateral")
        self.assertEqual(typ("What will Donald Trump say during Cabinet Meeting?",
                             "Cabinet Meeting"), "cabinet")
        self.assertEqual(typ("What will Donald Trump say during Remarks at Mack Trucks?",
                             "Remarks at Mack Trucks"), "remarks")

    def test_event_ts(self):
        day = datetime(2026, 10, 21, 16, tzinfo=timezone.utc).timestamp()
        self.assertEqual(tmf.event_ts("KXTRUMPMENTION-26OCT21"), day)
        self.assertEqual(tmf.event_ts("KXTRUMPMENTIONB-26OCT21B"), day)
        late = {"close_time": "2026-10-23T03:00:00Z"}
        self.assertEqual(tmf.event_ts("KXTRUMPMENTION-26OCT21", late), day)
        early = {"close_time": "2025-06-15T14:00:00Z"}       # a ticker dated after its event
        self.assertEqual(tmf.event_ts("KXTRUMPMENTION-25DEC31", early),
                         datetime(2025, 6, 15, 14, tzinfo=timezone.utc).timestamp())


class TestModel(Base):

    def test_price_formula(self):
        """One rally: 'foo' said, 'bar' not, both just now (weights 1):
        p_all = base = 0.5; foo's lift logit(3.5/6); p = (1 + 3 prior) / 4."""
        now = T0.timestamp()
        hist = {"E1": {"t": now, "type": "rally", "w": {"foo": 1, "bar": 0}}}
        model = tmf.fit(hist, now)
        self.assertAlmostEqual(model["base"]["rally"], 0.5)
        p, d = tmf.price(model, "foo", "rally")
        self.assertAlmostEqual(p, (1 + 3 * 3.5 / 6) / 4)           # 0.6875
        self.assertAlmostEqual(tmf.price(model, "bar", "rally")[0], (3 * 2.5 / 6) / 4)  # 0.3125
        self.assertEqual((d["n_eff"], d["n_type"], d["base"]), (1.0, 1.0, 0.5))
        # another type: no word x type record, so the lifted base rate
        self.assertAlmostEqual(tmf.price(model, "foo", "interview")[0], 3.5 / 6)
        # a word never listed: the type's base rate, n_eff 0 (the wide prior)
        p, d = tmf.price(model, "never listed", "rally")
        self.assertAlmostEqual(p, 0.5)
        self.assertEqual(d["n_eff"], 0.0)

    def test_recent_events_weigh_more(self):
        now = T0.timestamp()
        old = now - 90 * 86400

        def p(recent_y, old_y):
            hist = {"NEW": {"t": now, "type": "rally", "w": {"foo": recent_y, "x": 0, "z": 1}},
                    "OLD": {"t": old, "type": "rally", "w": {"foo": old_y, "x": 0, "z": 1}}}
            return tmf.price(tmf.fit(hist, now), "foo", "rally")[0]
        self.assertGreater(p(1, 0), p(0, 1) + 0.15)
        # no decay at all (huge half-lives): the order of the two no longer matters
        with mock.patch.object(tmf, "HALF_LIFE_WORD_TYPE_DAYS", 1e9), \
                mock.patch.object(tmf, "HALF_LIFE_DAYS", 1e9):
            self.assertAlmostEqual(p(1, 0), p(0, 1))

    def test_clipped(self):
        now = T0.timestamp()
        hist = {f"E{i}": {"t": now, "type": "rally", "w": {"always": 1, "never": 0}}
                for i in range(200)}
        model = tmf.fit(hist, now)
        self.assertEqual(tmf.price(model, "always", "rally")[0], tmf.P_MAX)
        self.assertEqual(tmf.price(model, "never", "rally")[0], tmf.P_MIN)

    def test_duds(self):
        ten_no = {f"w{i}": 0 for i in range(10)}
        self.assertTrue(tmf.is_dud(ten_no))
        self.assertFalse(tmf.is_dud({f"w{i}": 0 for i in range(9)}))
        self.assertFalse(tmf.is_dud(dict(ten_no, **{tmf.EDQ_KEY: 0})))
        self.assertTrue(tmf.is_dud({tmf.EDQ_KEY: 1, "w": 0}))
        now = T0.timestamp()
        hist = {f"E{i}": {"t": now, "type": "rally",
                          "w": {tmf.EDQ_KEY: int(i == 0), "foo": int(i != 0)}} for i in range(4)}
        model = tmf.fit(hist, now)
        dud_all = (1 + 1.0) / (4 + 20.0)
        p, d = tmf.price(model, tmf.EDQ_KEY, "rally")
        self.assertAlmostEqual(p, (1 + 10 * dud_all) / (4 + 10))
        self.assertIsNone(d["n_eff"])
        # the "Event does not qualify" market is not a word: out of the word counts
        self.assertNotIn(tmf.EDQ_KEY, model["Nk"])

    def test_absorb(self):
        hist = {}
        ms = settled_event("KXTRUMPMENTION-26OCT20", {"Biden": "yes", "Gas / Oil": "no"},
                           datetime(2026, 10, 20, 23, tzinfo=timezone.utc))
        ms.append(mk("KXTRUMPMENTION-26OCT20", "Pending", "", status="active"))
        ms.append(mk("KXTRUMPMENTION-26APR25", "Karoline", "scalar"))     # a scalar settlement
        added, newest = tmf.absorb(hist, ms)
        self.assertEqual(added, 2)
        self.assertEqual(list(hist), ["KXTRUMPMENTION-26OCT20"])
        e = hist["KXTRUMPMENTION-26OCT20"]
        self.assertEqual(e["w"], {"biden": 1, "gas / oil": 0})
        self.assertEqual(e["type"], "rally")
        self.assertEqual(newest, datetime(2026, 10, 21, 3, 30, tzinfo=timezone.utc).timestamp())
        self.assertEqual(tmf.absorb(hist, ms), (0, newest))              # idempotent


class TestFile(Base):

    def kalshi(self, page=0):
        hist = []
        for i, day in enumerate((20, 21, 22, 23)):
            hist += settled_event(f"KXTRUMPMENTION-26OCT{day}",
                                  {"Biden": "yes", "Crypto / Bitcoin": "no", "Hormuz": "yes" if i % 2 else "no",
                                   "Event does not qualify": "no"},
                                  datetime(2026, 10, day, 23, tzinfo=timezone.utc))
        archive = settled_event("KXTRUMPMENTION-26JUL14", {"Biden": "no", "Hormuz": "yes"},
                                datetime(2026, 7, 14, 23, tzinfo=timezone.utc))
        live = open_event("KXTRUMPMENTION-26OCT25", ["Biden"])
        live["markets"].append(mk("KXTRUMPMENTION-26OCT25", "Hormuz", "yes", status="finalized"))
        events = [open_event("KXTRUMPMENTION-26OCT27", ["Biden", "Bitcoin / Crypto", "France",
                                                         "Event does not qualify"]),
                  open_event("KXTRUMPMENTIONB-26OCT27", ["Hormuz"]), live]
        return FakeKalshi(hist, archive, events, page=page)

    def test_cold_start_full_read(self):
        k = self.kalshi(page=2)
        ok, miss = tmf.write_fair_file(self.path, now=T0, get_text=k)
        self.assertEqual((ok, miss), (5, 1))
        self.assertIn("/historical/markets", k.paths())
        self.assertTrue(any(p == "/markets" and "min_settled_ts" not in q for p, q in k.calls))
        self.assertTrue(any(q.get("cursor") for _, q in k.calls))          # paged
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        js = self.read()
        m = js["markets"]
        self.assertEqual(sorted(m), sorted(t for e in k.open_events[:2] for t in
                                           (x["ticker"] for x in e["markets"])))
        for t, row in m.items():
            self.assertTrue(0 < row["p"] < 1)
            self.assertEqual(row["fetched_at"], iso(T0))
            self.assertIn(row["event"], ("KXTRUMPMENTION-26OCT27", "KXTRUMPMENTIONB-26OCT27"))
        by_word = {row["word"]: row for row in m.values()}
        self.assertGreater(by_word["Biden"]["p"], 0.6)                    # said 4 of 5
        self.assertLess(by_word["Bitcoin / Crypto"]["p"], 0.35)           # never said, keyed as "Crypto / Bitcoin"
        self.assertEqual(by_word["France"]["n_eff"], 0.0)                 # never listed: the wide prior
        self.assertAlmostEqual(by_word["France"]["p"], by_word["France"]["base"], places=3)
        self.assertIsNone(by_word["Event does not qualify"]["n_eff"])
        self.assertLess(by_word["Event does not qualify"]["p"], 0.1)
        # the event under way (a market already settled) gets nothing
        self.assertEqual(js["event_fair"]["KXTRUMPMENTION-26OCT25"]["why"], "under way or over")
        self.assertFalse(any(r["event"] == "KXTRUMPMENTION-26OCT25" for r in m.values()))
        self.assertEqual(len(js["history"]), 5)
        self.assertEqual(js["history_at"], T0.timestamp())

    def test_history_ttl_and_incremental_read(self):
        k = self.kalshi()
        tmf.write_fair_file(self.path, now=T0, get_text=k)
        through = self.read()["history_through"]
        k.calls.clear()
        t1 = T0 + timedelta(minutes=10)
        tmf.write_fair_file(self.path, now=t1, get_text=k)
        self.assertNotIn("/markets", k.paths())                           # history fresh
        self.assertIn("/events", k.paths())                               # events every 10 min
        self.assertEqual(self.read()["markets"][k.open_events[0]["markets"][0]["ticker"]]
                         ["fetched_at"], iso(T0))                         # the older read
        k.settled += settled_event("KXTRUMPMENTION-26OCT24", {"France": "yes"},
                                   datetime(2026, 10, 26, 2, tzinfo=timezone.utc))
        k.calls.clear()
        t2 = T0 + timedelta(hours=1, minutes=5)
        tmf.write_fair_file(self.path, now=t2, get_text=k)
        reads = [q for p, q in k.calls if p == "/markets"]
        self.assertEqual(len(reads), 2)                                   # one per series
        self.assertTrue(all(q["min_settled_ts"] == int(through - tmf.HISTORY_OVERLAP_SECS)
                            for q in reads))
        self.assertNotIn("/historical/markets", k.paths())
        js = self.read()
        self.assertEqual(js["history"]["KXTRUMPMENTION-26OCT24"]["w"]["france"], 1)
        france = [r for r in js["markets"].values() if r["word"] == "France"][0]
        self.assertGreater(france["n_eff"], 0.9)
        self.assertEqual(france["fetched_at"], iso(t2))

    def test_failed_history_read_never_looks_fresh(self):
        k = self.kalshi()
        tmf.write_fair_file(self.path, now=T0, get_text=k)
        before = self.read()
        k.fail.add("/markets")
        t1 = T0 + timedelta(hours=2)
        ok, _ = tmf.write_fair_file(self.path, now=t1, get_text=k)
        js = self.read()
        self.assertEqual(ok, 5)                                           # still priced ...
        self.assertTrue(all(r["fetched_at"] == iso(T0) for r in js["markets"].values()))  # ... but old
        self.assertIn("settled-history read failed", self.log.call_args_list[-1].args[0])
        self.assertEqual(js["history_at"], T0.timestamp())
        self.assertEqual(js["history"], before["history"])
        self.assertEqual(js["events_at"], t1.timestamp())

    def test_failed_event_read_reuses_cache_then_ages(self):
        k = self.kalshi()
        tmf.write_fair_file(self.path, now=T0, get_text=k)
        k.fail.add("/events")
        t1 = T0 + timedelta(hours=1, minutes=30)
        ok, _ = tmf.write_fair_file(self.path, now=t1, get_text=k)
        js = self.read()
        self.assertEqual(ok, 5)
        self.assertEqual(js["history_at"], t1.timestamp())                # history read fine
        self.assertTrue(all(r["fetched_at"] == iso(T0) for r in js["markets"].values()))

    def test_no_reads_at_all_writes_no_fairs(self):
        k = self.kalshi()
        k.fail.update({"/markets", "/historical/markets", "/events"})
        self.assertEqual(tmf.write_fair_file(self.path, now=T0, get_text=k), (0, 0))
        js = self.read()
        self.assertEqual(js["markets"], {})
        self.assertEqual(js["history_at"], 0.0)

    def test_history_but_no_event_read_yet(self):
        k = self.kalshi()
        k.fail.add("/events")
        self.assertEqual(tmf.write_fair_file(self.path, now=T0, get_text=k), (0, 0))
        self.assertEqual(self.read()["markets"], {})

    def test_seed_starts_warm_but_stale_until_read(self):
        k = self.kalshi()
        hist = {}
        _, through = tmf.absorb(hist, k.settled)
        seed_at = (T0 - timedelta(days=2)).timestamp()
        with open(tmf.SEED_FILE, "w", encoding="utf-8") as f:
            json.dump({"history": hist, "history_through": through, "history_at": seed_at}, f)
        k.fail.add("/markets")
        ok, _ = tmf.write_fair_file(self.path, now=T0, get_text=k)
        self.assertEqual(ok, 5)
        js = self.read()
        self.assertTrue(all(r["fetched_at"] == tmf._iso(seed_at) for r in js["markets"].values()))
        self.assertNotIn("/historical/markets", k.paths())                # the seed is not cold
        k.fail.clear()
        k.calls.clear()
        tmf.write_fair_file(self.path, now=T0 + timedelta(minutes=1), get_text=k)
        self.assertTrue(any(p == "/markets" and "min_settled_ts" in q for p, q in k.calls))
        self.assertTrue(all(r["fetched_at"] == iso(T0) for r in self.read()["markets"].values()))

    def test_signed_reader_first_public_on_failure(self):
        k = self.kalshi()

        def signed(path, params):
            return json.loads(k(tmf.KALSHI_API + path, params))
        public = mock.Mock(side_effect=AssertionError("public read"))
        ok, _ = tmf.write_fair_file(self.path, now=T0, get_json=signed, get_text=public)
        self.assertEqual(ok, 5)
        public.assert_not_called()

        def broken(path, params):
            raise RuntimeError("signing failed")
        os.remove(self.path)
        k.calls.clear()
        ok, _ = tmf.write_fair_file(self.path, now=T0, get_json=broken, get_text=k)
        self.assertEqual(ok, 5)
        self.assertIn("/events", k.paths())

    def test_min_history_omits_unseen_words(self):
        k = self.kalshi()
        with mock.patch.object(tmf, "MIN_HISTORY_N", 0.5):
            ok, _ = tmf.write_fair_file(self.path, now=T0, get_text=k)
        js = self.read()
        self.assertEqual(ok, 4)
        self.assertFalse(any(r["word"] == "France" for r in js["markets"].values()))
        self.assertEqual(len(js["event_fair"]["KXTRUMPMENTION-26OCT27"]["omitted"]), 1)
        self.assertTrue(any(r["word"] == "Event does not qualify" for r in js["markets"].values()))

    def test_old_history_pruned(self):
        k = self.kalshi()
        tmf.write_fair_file(self.path, now=T0, get_text=k)
        later = T0 + timedelta(days=tmf.KEEP_DAYS - 90)
        tmf.write_fair_file(self.path, now=later, get_text=k)
        self.assertNotIn("KXTRUMPMENTION-26JUL14", self.read()["history"])
        self.assertIn("KXTRUMPMENTION-26OCT23", self.read()["history"])

    def test_open_event_statuses(self):
        ev = "KXTRUMPMENTION-26OCT28"
        listing = open_event(ev, ["Biden", "Hormuz"])
        listing["markets"].append(mk(ev, "Later", status="initialized"))   # not open yet
        k = FakeKalshi(open_events=[listing])
        got = tmf.fetch_open(get_text=k)[ev]
        self.assertFalse(got["live"])
        self.assertEqual(sorted(m["word"] for m in got["markets"].values()), ["Biden", "Hormuz"])
        self.assertEqual(got["type"], "rally")
        listing["markets"][0]["status"] = "closed"                         # the event ended
        self.assertTrue(tmf.fetch_open(get_text=FakeKalshi(open_events=[listing]))[ev]["live"])

    def test_page_limit_is_a_failed_read(self):
        k = self.kalshi(page=1)
        with mock.patch.object(tmf, "MAX_PAGES", 2):
            with self.assertRaises(RuntimeError):
                tmf.fetch_settled(None, get_text=k)

    def test_main_dry_run(self):
        k = self.kalshi()

        def public(path, params, get_text=None):
            return json.loads(k(tmf.KALSHI_API + path, params))
        out = os.path.join(self.dir, "dry.json")
        with mock.patch.object(tmf, "_http_json", side_effect=public), \
                mock.patch("sys.stdout") as stdout:
            self.assertEqual(tmf.main(["--out", out, "--tol", "15"]), 0)
        printed = "".join(c.args[0] for c in stdout.write.call_args_list)
        self.assertIn("5 markets priced, 1 open events without a fair", printed)
        self.assertIn("book 60.0x65.0", printed)
        self.assertTrue(os.path.exists(out))
        self.assertNotIn("run-logs", tmf.DEFAULT_OUT)


class TestSeed(unittest.TestCase):

    def test_shipped_seed(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(tmf.__file__)),
                               "trump_mention_seed.json"), encoding="utf-8") as f:
            seed = json.load(f)
        hist = seed["history"]
        self.assertGreater(len(hist), 100)
        self.assertGreater(seed["history_through"], 0)
        self.assertGreater(seed["history_at"], 0)
        types = {t for t, _ in tmf._TYPES} | {"remarks"}
        for ev, e in hist.items():
            self.assertTrue(ev.startswith(tmf.SERIES))
            self.assertIn(e["type"], types)
            self.assertTrue(e["w"])
            self.assertTrue(all(y in (0, 1) for y in e["w"].values()))
            self.assertTrue(all(k == tmf.norm_word(k) for k in e["w"]))
        model = tmf.fit(hist, seed["history_at"])
        crypto, _ = tmf.price(model, tmf.norm_word("Crypto / Bitcoin"), "rally")
        save, _ = tmf.price(model, tmf.norm_word("Save America Act"), "rally")
        self.assertFalse(math.isnan(crypto) or math.isnan(save))
        self.assertLess(crypto, 0.2)          # never said at a 2026 rally
        self.assertGreater(save, 0.7)         # the stump speech's line


if __name__ == "__main__":
    unittest.main()
