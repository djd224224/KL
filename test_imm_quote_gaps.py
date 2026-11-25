"""Tests for imm_quote_gaps (the morning "IMM quotes and overrides" email).

Its own file: importing imm_quote_gaps applies the live launcher's env."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

import imm_quote_gaps as g

NOW = datetime(2026, 10, 7, 11, 20, tzinfo=timezone.utc)


class TestConfigGapLines(unittest.TestCase):
    """Jack 2026-10-07: "also resolve issues like this going forward" -- the
    live bot's hand-config gaps go under ACTION NEEDED, one line a series."""

    def test_one_line_per_series(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "config_gaps.json")
            self.assertEqual(g.config_gap_lines(NOW, p), [])        # no file
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"updated_at": "2026-10-07T11:00:00Z", "gaps": {
                    "KXA-27": {"series": "KXA", "msg": "no date"},
                    "KXA-28": {"series": "KXA", "msg": "no date"},
                    "KXB-26": {"series": "KXB", "msg": "no election day"}}}, f)
            lines = g.config_gap_lines(NOW, p)
            self.assertIn("3 event(s) in 2 series", lines[0])
            self.assertIn("2026-10-07T11:00:00Z", lines[0])
            self.assertEqual(lines[1:], ["  KXA (+1 more): no date",
                                         "  KXB: no election day", ""])
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"gaps": {}}, f)
            self.assertEqual(g.config_gap_lines(NOW, p), [])
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(g.config_gap_lines(NOW, p), [])


class _Meta:
    def __init__(self, ticker):
        self.ticker = ticker
        self.est_dollars_per_day = 0.0


class _BatchClient:
    """GET /markets/orderbooks: up to 100 tickers a call, books in the single
    read's shape; `fail_on` makes that call (1-based) raise; `drop` leaves
    tickers out of the answer."""
    markets_url = "/markets"

    def __init__(self, fail_on=None, drop=()):
        self.calls = []
        self.fail_on = fail_on
        self.drop = set(drop)

    def get(self, path, params=None):
        self.calls.append((path, list((params or {}).get("tickers") or [])))
        if self.fail_on and len(self.calls) == self.fail_on:
            raise RuntimeError("HTTP 503")
        return {"orderbooks": [{"ticker": t, "orderbook_fp": {"yes_dollars": [["0.4000", "10.00"]],
                                                              "no_dollars": []}}
                               for t in self.calls[-1][1] if t not in self.drop]}


class _Bot:
    """The scan bot's estimator: reads the candidate book through
    _read_candidate_book (a class method, like the real bot's); a batch book
    earns $1/day, a single read $0.50, so tests can tell the paths apart."""
    def __init__(self):
        self.single_reads = []

    def _read_candidate_book(self, ticker):
        self.single_reads.append(ticker)
        return {"orderbook": {"yes": [[40, 5]], "no": []}}

    def _estimate_candidate_yield(self, meta, _held):
        ob = self._read_candidate_book(meta.ticker)
        meta.est_dollars_per_day = 1.0 if "orderbook_fp" in ob else 0.5
        return True


def _rows(n, start=0):
    return [{"ticker": f"KXT-{i}", "meta": _Meta(f"KXT-{i}"), "reason": None, "est": None}
            for i in range(start, start + n)]


class TestBatchBooks(unittest.TestCase):
    """R-261124-22 (Jack 2026-11-25: "Yes, all four"): every unquoted book is
    read 100 a call, and the scan bot's single read is only borrowed."""

    def test_fetch_books_keeps_the_single_read_shape(self):
        c = _BatchClient()
        got = g.fetch_books(c, ["A", "B"])
        self.assertEqual(c.calls, [("/markets/orderbooks", ["A", "B"])])
        self.assertEqual(set(got), {"A", "B"})
        self.assertEqual(set(got["A"]), {"orderbook_fp"})           # no "ticker" key

    def test_every_row_is_estimated_on_batch_books_and_the_read_is_restored(self):
        c, bot, rows = _BatchClient(), _Bot(), _rows(250)
        out = g.estimate_batched(c, bot, rows)
        self.assertEqual((out["calls"], out["books"], out["error"]), (3, 250, None))
        self.assertEqual([len(t) for _p, t in c.calls], [100, 100, 50])
        self.assertTrue(all(r["est"] == 1.0 and r["tried"] for r in rows))
        self.assertEqual(bot.single_reads, [])
        self.assertNotIn("_read_candidate_book", vars(bot))          # class method back in charge
        self.assertEqual(bot._estimate_candidate_yield(_Meta("X"), []), True)
        self.assertEqual(bot.single_reads, ["X"])

    def test_a_failed_call_leaves_the_rest_to_single_reads(self):
        c, bot, rows = _BatchClient(fail_on=2), _Bot(), _rows(250)
        out = g.estimate_batched(c, bot, rows)
        self.assertEqual((out["calls"], out["books"]), (2, 100))
        self.assertIn("HTTP 503", out["error"])
        self.assertTrue(all(r.get("tried") for r in rows[:100]))
        self.assertFalse(any(r.get("tried") for r in rows[100:]))
        self.assertNotIn("_read_candidate_book", vars(bot))

    def test_call_cap_and_missing_tickers(self):
        c, bot, rows = _BatchClient(drop={"KXT-3"}), _Bot(), _rows(150)
        out = g.estimate_batched(c, bot, rows, max_calls=1)
        self.assertEqual((out["calls"], out["books"]), (1, 99))
        self.assertFalse(rows[3].get("tried"))                     # not in the answer
        self.assertFalse(any(r.get("tried") for r in rows[100:]))   # past the cap
        self.assertEqual(bot.single_reads, [])

    def test_unreadable_book_is_labelled(self):
        class _NoBot(_Bot):
            def _estimate_candidate_yield(self, meta, _held):
                self._read_candidate_book(meta.ticker)
                return False
        rows = _rows(2)
        g.estimate_batched(_BatchClient(), _NoBot(), rows)
        self.assertEqual([r["reason"] for r in rows], ["book unreadable"] * 2)


if __name__ == "__main__":
    unittest.main()
