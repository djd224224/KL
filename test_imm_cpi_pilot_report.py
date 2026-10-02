"""Tests for imm_cpi_pilot_report's marks (the Oct 13 CPI pilot review).

Kalshi reads an empty YES ask as $1.00 and an empty bid as $0, in a market
read and in a candlestick's closes alike. The report took (bid + ask) / 2
whenever both read above 0, for the current mark and for the hourly candles
behind the +1h / +24h mark-outs, so a stray bid under no offer marked half
way to $1 (2026-10-01).

imm_cpi_pilot_report is imported inside setUp, as the other report tests
import theirs."""
import unittest


def _candle(bid, ask, close=None, previous=None):
    price = {}
    if close is not None:
        price["close_dollars"] = close
    if previous is not None:
        price["previous_dollars"] = previous
    return {"end_period_ts": 1_790_000_000, "yes_bid": {"close_dollars": bid},
            "yes_ask": {"close_dollars": ask}, "price": price}


class _ReportTest(unittest.TestCase):
    def setUp(self):
        import imm_cpi_pilot_report as cpi
        self.cpi = cpi


class YesMarkTests(_ReportTest):
    def test_a_missing_side_is_never_a_price(self):
        ym = self.cpi.yes_mark
        self.assertAlmostEqual(ym(0.26, 0.38, 0.33), 0.32)           # two-sided: the mid
        self.assertAlmostEqual(ym(0.05, 1.00, 0.97), 0.97)           # no offer: was 0.525
        self.assertAlmostEqual(ym(0.32, 1.00, 0.20), 0.32)           # ... floored at the bid
        self.assertAlmostEqual(ym(0.0, 0.40, 0.55), 0.40)            # no bid: capped at the ask
        self.assertAlmostEqual(ym(0.0, 1.00, 0.82), 0.82)            # empty book: the last trade
        self.assertAlmostEqual(ym(0.98, 0.9999, 0.50), 0.98995)      # a real sub-penny offer
        for bid, ask in ((0.05, 1.00), (0.0, 0.40), (0.0, 1.00)):
            self.assertIsNone(ym(bid, ask, 0.0), (bid, ask))

    def test_a_50c_wide_book_marks_the_last_trade_inside_the_touch(self):
        # Jack 2026-10-01, "Yes, at 50c+" (incentive_mm.MARK_WIDE_SPREAD_CENTS)
        ym = self.cpi.yes_mark
        self.assertAlmostEqual(ym(0.01, 0.70, 0.01), 0.01)           # not the 35.5c mid
        self.assertAlmostEqual(ym(0.20, 0.70, 0.05), 0.20)
        self.assertAlmostEqual(ym(0.20, 0.69, 0.90), 0.445)          # 49c wide: the mid
        self.assertAlmostEqual(ym(0.01, 0.99, 0.0), 0.50)            # never traded: the mid
        self.addCleanup(setattr, self.cpi, "MARK_WIDE_SPREAD_CENTS", self.cpi.MARK_WIDE_SPREAD_CENTS)
        self.cpi.MARK_WIDE_SPREAD_CENTS = 0                          # the bot's kill switch
        self.assertAlmostEqual(ym(0.01, 0.70, 0.01), 0.355)


class CandleMarkTests(_ReportTest):
    def test_the_cold_brew_candles(self):
        # KXDDCOLDBREW-26OCT02-T4.45's hourly candles, read 2026-10-01: no
        # trades in either hour, last trade 97c before them
        self.assertAlmostEqual(self.cpi.candle_mark(
            _candle("0.0500", "1.0000", previous="0.9700")), 0.97)          # was 0.525
        self.assertAlmostEqual(self.cpi.candle_mark(
            _candle("0.9800", "1.0000", previous="0.9700")), 0.98)          # was 0.99

    def test_a_two_sided_close_is_the_mid(self):
        # KXCPICORE-26NOV-T0.3, the hour to 2026-09-29 12Z
        self.assertAlmostEqual(self.cpi.candle_mark(
            _candle("0.2300", "0.5000", close="0.3300", previous="0.3200")), 0.365)
        # 50c+ wide: the last trade inside the touch
        self.assertAlmostEqual(self.cpi.candle_mark(
            _candle("0.0100", "0.7000", previous="0.0100")), 0.01)

    def test_the_hour_s_own_trade_beats_the_one_before(self):
        self.assertAlmostEqual(self.cpi.candle_mark(
            _candle("0.2000", "1.0000", close="0.2500", previous="0.9000")), 0.25)

    def test_an_empty_book_is_the_last_trade_and_no_trade_is_no_mark(self):
        self.assertAlmostEqual(self.cpi.candle_mark(
            _candle("0.0000", "1.0000", previous="0.9700")), 0.97)
        self.assertAlmostEqual(self.cpi.candle_mark({"price": {"previous_dollars": "0.4000"}}), 0.40)
        self.assertIsNone(self.cpi.candle_mark(_candle("0.0500", "1.0000")))
        self.assertIsNone(self.cpi.candle_mark({}))


if __name__ == "__main__":
    unittest.main()
