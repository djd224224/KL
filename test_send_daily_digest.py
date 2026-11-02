"""Tests for send_daily_digest's marks (the crypto fleet digest, 2026-10-01).

Kalshi reads an empty YES ask as $1.00 and an empty bid as $0, and
market_mids took (bid + ask) / 2 whenever both read above 0, so a stray bid
under no offer marked half way to $1. It now marks the way the IMM's
incentive_mm.bulk_mark_cents does, through a local yes_mark.

And the all-time cumulative (2026-10-08): Kalshi's settlements API keeps
only ~66 days, so the July monthly's 67 tickers (+$566.27) silently left the
headline. Crypto tickers the API no longer returns now come from
Kalshi-Settlements-archive.csv.

send_daily_digest is imported inside setUp: importing it copies the alert
credentials into os.environ and imports crypto_touch_mm, and a module-level
import would do that during discovery, ahead of other test modules."""
import csv
import os
import tempfile
import unittest
from datetime import date, datetime, timezone

from merge_kalshi_archive import CSV_COLUMNS

EV = "KXBTCD-26OCT0217"


def _m(strike, bid, ask, last):
    return {"ticker": f"{EV}-{strike}", "yes_bid_dollars": bid,
            "yes_ask_dollars": ask, "last_price_dollars": last}


class _Client:
    """get_markets / get_positions for one event over fixed records."""

    def __init__(self, markets, positions=()):
        self.markets, self.positions = list(markets), list(positions)

    def get_markets(self, event_ticker="", limit=None):
        return {"markets": [m for m in self.markets
                            if m["ticker"].startswith(event_ticker + "-")]}

    def get_positions(self, event_ticker="", limit=None):
        return {"event_positions": [{"event_ticker": event_ticker,
                                     "realized_pnl_dollars": "1.50",
                                     "fees_paid_dollars": "0.10",
                                     "event_exposure_dollars": "6.00"}],
                "market_positions": self.positions}


class _DigestTest(unittest.TestCase):
    def setUp(self):
        import send_daily_digest as dd
        self.dd = dd


class YesMarkTests(_DigestTest):
    def test_a_missing_side_is_never_a_price(self):
        ym = self.dd.yes_mark
        self.assertAlmostEqual(ym(0.30, 0.34, 0.20), 0.32)           # two-sided: the mid
        self.assertAlmostEqual(ym(0.05, 1.00, 0.97), 0.97)           # no offer: was 0.525
        self.assertAlmostEqual(ym(0.32, 1.00, 0.20), 0.32)           # ... floored at the bid
        self.assertAlmostEqual(ym(0.0, 0.40, 0.55), 0.40)            # no bid: capped at the ask
        self.assertAlmostEqual(ym(0.0, 0.40, 0.30), 0.30)
        self.assertAlmostEqual(ym(0.0, 1.00, 0.82), 0.82)            # empty book: the last trade
        self.assertAlmostEqual(ym(0.98, 0.9999, 0.50), 0.98995)      # a real sub-penny offer
        for bid, ask in ((0.05, 1.00), (0.0, 0.40), (0.0, 1.00)):
            self.assertIsNone(ym(bid, ask, 0.0), (bid, ask))         # nothing to go on

    def test_a_50c_wide_book_marks_the_last_trade_inside_the_touch(self):
        # Jack 2026-10-01, "Yes, at 50c+" (incentive_mm.MARK_WIDE_SPREAD_CENTS)
        ym = self.dd.yes_mark
        self.assertAlmostEqual(ym(0.01, 0.70, 0.01), 0.01)           # not the 35.5c mid
        self.assertAlmostEqual(ym(0.20, 0.70, 0.90), 0.70)
        self.assertAlmostEqual(ym(0.20, 0.69, 0.90), 0.445)          # 49c wide: the mid
        self.assertAlmostEqual(ym(0.01, 0.99, 0.0), 0.50)            # never traded: the mid
        self.assertAlmostEqual(ym(0.01, 0.99, 0.20), ym(0.01, 1.00, 0.20))   # a 99c flicker
        self.addCleanup(setattr, self.dd, "MARK_WIDE_SPREAD_CENTS", self.dd.MARK_WIDE_SPREAD_CENTS)
        self.dd.MARK_WIDE_SPREAD_CENTS = 0                           # the bot's kill switch
        self.assertAlmostEqual(ym(0.01, 0.70, 0.01), 0.355)


class MarketMidsTests(_DigestTest):
    def test_one_sided_books_mark_off_the_last_trade(self):
        client = _Client([_m("T118000", "0.3000", "0.3400", "0.2000"),
                          _m("T112000", "0.0500", "1.0000", "0.9700"),
                          _m("T124000", "0.0000", "0.0400", "0.3000"),
                          _m("T130000", "0.0100", "1.0000", "0.0000")])
        mids = self.dd.market_mids(client, EV)
        self.assertEqual({t[len(EV) + 1:]: round(v, 4) for t, v in mids.items()},
                         {"T118000": 0.32, "T112000": 0.97, "T124000": 0.04})

    def test_event_pnl_values_a_no_offer_long_at_the_last_trade(self):
        # 10 YES held at a $6.00 cost under a stray 5c bid with no offer and a
        # 97c last trade: +3.70, where the 52.5c mid said -0.75
        client = _Client([_m("T112000", "0.0500", "1.0000", "0.9700")],
                         [{"ticker": f"{EV}-T112000", "position": "10",
                           "market_exposure_dollars": "6.00"}])
        out = self.dd.event_pnl(client, EV)
        self.assertAlmostEqual(out["unrealized"], 3.70)
        self.assertEqual((out["net_pos"], out["realized"], out["ok"]), (10.0, 1.5, True))


JUL_MAX = "KXBTCMAXMON-BTC-26JUL31-6500000"     # aged out of the API on 10/08
AUG_MAX = "KXBTCMAXMON-BTC-26AUG31-8000000"     # still in it
WEEKLY = "KXBTCD-26OCT0217-T118000"              # a Friday 5pm close


def _arow(ticker, settled, result, yes_n, yes_avg, no_n, no_avg, profit):
    """One Kalshi-Settlements-archive.csv row in merge_kalshi_archive's
    format: floored counts, the *_In_Cents averages in dollars, and
    Profit_In_Dollars = fetch_settlements_csv's gross `revenue`."""
    row = dict.fromkeys(CSV_COLUMNS, "")
    row.update({"type": "Settlement", "Original_Date": settled, "Market_Ticker": ticker,
                "Yes_Contracts_Owned": str(yes_n), "Yes_Contracts_Average_Price_In_Cents": yes_avg,
                "No_Contracts_Owned": str(no_n), "No_Contracts_Average_Price_In_Cents": no_avg,
                "Result": result, "Profit_In_Dollars": profit})
    return row


def _api(ticker, settled, result, yes_n, yes_cost, no_n="0", no_cost="0"):
    """One GET /portfolio/settlements record."""
    return {"ticker": ticker, "settled_time": settled, "market_result": result,
            "yes_count_fp": yes_n, "yes_total_cost_dollars": yes_cost,
            "no_count_fp": no_n, "no_total_cost_dollars": no_cost}


class _SweepClient:
    """One page each of the two sweeps cumulative_family_realized makes, and
    a fully settled answer to recent_settlement_rows' get_event probe."""

    def __init__(self, settlements, unsettled=()):
        self.settlements, self.unsettled = list(settlements), list(unsettled)

    def get_portfolio_settlements(self, limit=None, cursor=None):
        return {"settlements": self.settlements}

    def get_positions(self, limit=None, settlement_status=None, cursor=None):
        return {"market_positions": self.unsettled}

    def get_event(self, event_ticker):
        return {"markets": [{"result": "yes"}, {"result": "no"}]}


class ArchiveTests(_DigestTest):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "Kalshi-Settlements-archive.csv")

    def _write(self, rows):
        with open(self.path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL,
                               lineterminator="\n")
            w.writeheader()
            w.writerows(rows)

    def test_archive_pnl_is_payout_minus_cost_not_the_profit_column(self):
        self._write([
            # the real row: 127 YES @ $0.78 + 24 NO @ $0.31, touched: +20.50
            # (Profit_In_Dollars is the 127.00 gross payout)
            _arow(JUL_MAX, "2026-07-15T07:10:40.838Z", "yes", 127, "0.78", 24, "0.31", "127.00"),
            _arow("KXETHMAXMON-ETH-26JUL31-400000", "2026-08-01T15:10:10.943Z", "no",
                  10, "0.20", 30, "0.70", "30.00"),                  # 30 - 2.00 - 21.00
            _arow("KXHIGHNY-26JUL20-B85.5", "2026-07-21T04:00:00Z", "yes",
                  50, "0.40", 0, "0.00", "50.00"),                   # not a fleet market
            _arow("KXBTCMINMON-BTC-26JUL31-5000000", "2026-08-01T15:10:20Z", "void",
                  5, "0.50", 0, "0.00", "2.50"),                     # no yes/no payout
        ])
        got = self.dd.archive_settlements(self.path)
        self.assertEqual(sorted(got), ["KXBTCMAXMON-BTC-26JUL31-6500000",
                                       "KXETHMAXMON-ETH-26JUL31-400000"])
        self.assertEqual(got[JUL_MAX][0],
                         datetime(2026, 7, 15, 7, 10, 40, 838000, tzinfo=timezone.utc))
        self.assertAlmostEqual(got[JUL_MAX][1], 20.50)
        self.assertAlmostEqual(got["KXETHMAXMON-ETH-26JUL31-400000"][1], 7.00)

    def test_aged_out_crypto_tickers_are_filled_in_from_the_archive(self):
        self._write([
            _arow(JUL_MAX, "2026-07-15T07:10:40.838Z", "yes", 127, "0.78", 24, "0.31", "127.00"),
            _arow(AUG_MAX, "2026-09-01T04:00:10Z", "no", 100, "0.40", 0, "0.00", "0.00"),
            _arow("KXHIGHNY-26JUL20-B85.5", "2026-07-21T04:00:00Z", "yes",
                  50, "0.40", 0, "0.00", "50.00"),
        ])
        client = _SweepClient(
            # AUG_MAX is in both: the API's -40.20 (100.5 YES) beats the archive's
            # floored -40.00
            [_api(AUG_MAX, "2026-09-01T04:00:10Z", "no", "100.50", "40.20"),
             _api(WEEKLY, "2026-10-02T21:00:05Z", "yes", "10", "6.00")],
            [{"ticker": "KXBTCMAXMON-BTC-26OCT31-9000000", "realized_pnl_dollars": "1.25"}])
        fams, recs, toks, arch = self.dd.cumulative_family_realized(client, self.path)
        self.assertAlmostEqual(fams["monthly"], 20.50 - 40.20 + 1.25)
        self.assertAlmostEqual(fams["weekly"], 4.00)
        self.assertAlmostEqual(toks[("BTC", "high")], 20.50 - 40.20 + 1.25)
        self.assertEqual((arch["n"], round(arch["pnl"], 2), arch["error"]), (1, 20.50, None))
        self.assertEqual(sorted((d.date(), f, label, ev, round(p, 2)) for d, f, label, ev, p in recs),
                         [(date(2026, 7, 15), "monthly", "BTC-MAX", "KXBTCMAXMON-BTC-26JUL31", 20.50),
                          (date(2026, 9, 1), "monthly", "BTC-MAX", "KXBTCMAXMON-BTC-26AUG31", -40.20),
                          (date(2026, 10, 2), "weekly", "BTC", "KXBTCD-26OCT0217", 4.00)])

    def test_recent_rows_sum_an_event_across_the_api_and_the_archive(self):
        # HYPE-MIN July: its 7/31 early touch has aged out, the 8/1 settle hasn't
        self._write([_arow("KXHYPEMINMON-HYPE-26JUL31-5250", "2026-07-31T20:20:51.035Z", "yes",
                           114, "0.39", 0, "0.00", "114.00")])           # +69.54
        client = _SweepClient([_api("KXHYPEMINMON-HYPE-26JUL31-5000",
                                    "2026-08-01T15:10:20.951Z", "no", "20", "4.00")])
        _fams, recs, _toks, _arch = self.dd.cumulative_family_realized(client, self.path)
        rows = self.dd.recent_settlement_rows(client, recs)
        self.assertEqual([(label, fam, day, round(pnl, 2)) for label, fam, day, pnl in rows],
                         [("HYPE-MIN", "monthly", date(2026, 8, 1), 65.54)])

    def test_a_missing_or_unreadable_archive_leaves_the_api_window_and_says_so(self):
        client = _SweepClient([_api(AUG_MAX, "2026-09-01T04:00:10Z", "no", "100.50", "40.20"),
                               _api(WEEKLY, "2026-10-02T21:00:05Z", "yes", "10", "6.00")])
        fams, _recs, _toks, arch = self.dd.cumulative_family_realized(
            client, os.path.join(os.path.dirname(self.path), "missing.csv"))
        self.assertEqual({k: round(v, 2) for k, v in fams.items()},
                         {"monthly": -40.20, "weekly": 4.00})
        self.assertEqual(arch["n"], 0)
        self.assertIn("FileNotFoundError", arch["error"])
        for content in ("",                                               # empty
                        '"type","Market_Id"\n"Settlement","x"\n'):        # not the archive's columns
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(content)
            got, _recs, _toks, arch = self.dd.cumulative_family_realized(client, self.path)
            self.assertEqual(got, fams, content)
            self.assertTrue(arch["error"], content)

    def test_an_api_only_total_gets_a_note_and_stays_out_of_the_history(self):
        ok = {"n": 67, "pnl": 566.27, "error": None}
        bad = {"n": 0, "pnl": 0.0, "error": "FileNotFoundError: no such file"}
        self.assertEqual(self.dd.cumulative_snap(-1202.17, ok),
                         {"cumulative": -1202.17, "cum_archive": 566.27})
        self.assertEqual(self.dd.cumulative_snap(-1768.44, bad), {})     # no d/d, not recorded
        self.assertEqual(self.dd.cumulative_snap(None, None), {})        # the sweep failed
        self.assertEqual(self.dd.archive_clause(ok),
                         "; the archive filled in 67 aged-out markets, +566.27")
        self.assertIn("UNREADABLE", self.dd.archive_clause(bad))
        self.assertEqual(self.dd.archive_clause(None), "")


def _cap_item(kind="risk_long", now=True, count=12, peak=463.0, limit=450.0, last=463.0,
              fleet="monthly", bot="BTC-MIN", where="KXBTCMINMON-BTC-26NOV30"):
    return {"fleet": fleet, "bot": bot, "kind": kind, "where": where, "limit": limit,
            "peak": peak, "peak_ts": "2026-11-02 08:00:00Z", "last": last,
            "ratio": peak / limit, "now": now, "first_at": "2026-11-02 08:00:00Z",
            "last_at": "2026-11-02 11:50:00Z", "secs_at": count * 30.0, "count": count}


def _cap_rep(hits=(), near=(), blind=()):
    return {"hits": list(hits), "near": list(near), "blind": list(blind),
            "top": (list(near) or [None])[0], "bots": 31, "window_hours": 24}


class CapAlertTests(_DigestTest):
    """Jack 2026-11-01: "make sure daily crypto email clearly alerts if any caps
    are hit": a red line per hit up top, an always-on caps line, a section."""

    def test_each_hit_is_a_top_warning(self):
        w = self.dd.cap_warnings(_cap_rep(hits=[_cap_item()]))
        self.assertEqual(len(w), 1)
        self.assertTrue(w[0].startswith("CAP HIT: BTC-MIN monthly -- $-at-risk cap, long side"))
        self.assertIn("$463 of $450; AT THE CAP NOW", w[0])

    def test_many_hits_are_capped_with_a_pointer(self):
        hits = [_cap_item(bot=f"B{i}") for i in range(self.dd.MAX_CAP_WARNINGS + 3)]
        w = self.dd.cap_warnings(_cap_rep(hits=hits))
        self.assertEqual(len(w), self.dd.MAX_CAP_WARNINGS + 1)
        self.assertIn("3 more cap hits", w[-1])

    def test_nothing_hit_means_no_warning_but_a_clean_caps_line(self):
        rep = _cap_rep(near=[_cap_item(count=0, now=False, peak=400.0, last=400.0)])
        self.assertEqual(self.dd.cap_warnings(rep), [])
        self.assertTrue(self.dd.crypto_caps.summary_text(rep).startswith("Caps: none hit"))

    def test_a_failed_or_blind_check_is_loud(self):
        self.assertIn("NOT checked", self.dd.cap_warnings(None)[0])
        w = self.dd.cap_warnings(_cap_rep(blind=["SOL-MIN monthly"]))
        self.assertIn("BLIND for SOL-MIN monthly", w[0])

    def test_section_lists_hits_near_and_limits(self):
        rep = _cap_rep(hits=[_cap_item()],
                       near=[_cap_item(kind="position", count=0, now=False, peak=250.0,
                                       limit=300.0, last=-250.0,
                                       where="KXSOLMINMON-SOL-26NOV30-8000|80")])
        text = "\n".join(self.dd.caps_text(rep))
        self.assertIn("== CAPS (last 24h) ==", text)
        self.assertIn("HIT   BTC-MIN monthly: $-at-risk cap, long side: $463 of $450", text)
        self.assertIn("near  BTC-MIN monthly: position cap on the 80 strike: 250 short of 300", text)
        self.assertIn("Limits: monthly 300/market", text)
        html = "".join(self.dd.caps_html(rep))
        self.assertIn(">HIT<", html)
        self.assertIn("$-at-risk cap, long side", html)
        self.assertEqual(self.dd.caps_text(_cap_rep()), [])
        self.assertEqual(self.dd.caps_html(None), [])


if __name__ == "__main__":
    unittest.main()
