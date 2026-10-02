"""Tests for send_daily_digest's marks (the crypto fleet digest, 2026-10-01).

Kalshi reads an empty YES ask as $1.00 and an empty bid as $0, and
market_mids took (bid + ask) / 2 whenever both read above 0, so a stray bid
under no offer marked half way to $1. It now marks the way the IMM's
incentive_mm.bulk_mark_cents does, through a local yes_mark.

send_daily_digest is imported inside setUp: importing it copies the alert
credentials into os.environ and imports crypto_touch_mm, and a module-level
import would do that during discovery, ahead of other test modules."""
import unittest

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


if __name__ == "__main__":
    unittest.main()
