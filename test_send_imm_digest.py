"""Tests for send_imm_digest's settlement valuation (2026-09-29).

Kalshi settles the NFL ladders / escalators (and the early-closed KXBKNUGGETS
strikes) as result "scalar" at a fractional settlement_value_dollars, and a
void refunds cost. The digest booked only yes / no: a scalar-settled position
stayed "open" and was marked at the last trade, because a settled book reads
0 / 100. KXNFLFFPTSLADDER-26SEP27LVNO-LVAJEANTY2 settled at 12c and was
carried at 82c; the bot was short 90 YES at 16c there, so the digest showed
-$59.40 on a position that made +$3.60.

send_imm_digest is imported inside setUp, as test_imm_pickoff does: importing
it mirrors the live launcher env into os.environ, and a module-level import
would do that during discovery, ahead of other test modules' imports."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

LADDER = "KXNFLFFPTSLADDER-26SEP27LVNO-LVAJEANTY2"


def _market(ticker, result="", status="active", value=None,
            bid="0.0000", ask="1.0000", last="0.8200"):
    """A Kalshi market record; the defaults are LVAJEANTY2's settled book."""
    m = {"ticker": ticker, "result": result, "status": status,
         "yes_bid_dollars": bid, "yes_ask_dollars": ask, "last_price_dollars": last}
    if value is not None:
        m["settlement_value_dollars"] = value
    return m


class _Client:
    """get_markets(tickers=..., limit=...) over fixed market records."""

    def __init__(self, *markets):
        self.markets = {m["ticker"]: m for m in markets}

    def get_markets(self, tickers="", limit=None):
        return {"markets": [self.markets[t] for t in tickers.split(",")
                            if t in self.markets]}


class _DigestTest(unittest.TestCase):
    def setUp(self):
        import send_imm_digest as sd
        self.sd = sd


class SettlementCentsTests(_DigestTest):
    def test_yes_no_pay_100_0_at_any_status(self):
        # unchanged: a yes / no result is booked as soon as Kalshi reports it
        for status in ("determined", "finalized", "closed"):
            self.assertEqual(self.sd.settlement_cents(_market("A", "yes", status)), 100.0)
            self.assertEqual(self.sd.settlement_cents(_market("A", "no", status)), 0.0)

    def test_finalized_scalar_pays_its_exact_value(self):
        m = _market(LADDER, "scalar", "finalized", "0.2160")
        self.assertEqual(self.sd.settlement_cents(m), 21.6)        # not 22
        self.assertEqual(self.sd.settlement_cents(_market(LADDER, "scalar", "settled", "0.1200")), 12.0)
        self.assertEqual(self.sd.settlement_cents(_market(LADDER, "Scalar", "FINALIZED", "0.0000")), 0.0)
        self.assertEqual(self.sd.settlement_cents(_market(LADDER, "scalar", "finalized", "1.0000")), 100.0)

    def test_scalar_and_void_wait_for_finalized(self):
        for status in ("determined", "closed", "active", ""):
            self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "scalar", status, "0.1200")))
            self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "void", status)))

    def test_scalar_without_a_readable_value_is_not_settled(self):
        for value in (None, "", "n/a", "1.5000", "-0.0100"):
            self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "scalar", "finalized", value)),
                              value)

    def test_void_and_unsettled(self):
        self.assertEqual(self.sd.settlement_cents(_market(LADDER, "void", "finalized")), self.sd.VOID)
        self.assertIsNone(self.sd.settlement_cents(_market(LADDER)))
        self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "", "finalized")))
        self.assertIsNone(self.sd.settlement_cents(_market(LADDER, "mystery", "finalized", "0.5")))


class CurrentMidsTests(_DigestTest):
    def test_settled_markets_are_results_open_ones_are_mids(self):
        client = _Client(_market(LADDER, "scalar", "finalized", "0.1200"),
                         _market("V", "void", "finalized", last="0.4000"),
                         _market("Y", "yes", "determined", bid="0.0000", ask="0.0000", last="0.9900"),
                         _market("N", "no", "finalized", last="0.0300"),
                         _market("D", "scalar", "determined", "0.1200"),
                         _market("O", bid="0.3000", ask="0.3400", last="0.2900"))
        mids, results = self.sd.current_mids(client, ["O", LADDER, "V", "Y", "N", "D"])
        self.assertEqual(results, {LADDER: 12.0, "V": self.sd.VOID, "Y": 100.0, "N": 0.0})
        # a settled book reads 0 / 100, so its "mid" is the last trade: 82c
        # here, which is exactly why a settled position is valued from results
        self.assertEqual(mids[LADDER], 82.0)
        self.assertEqual(mids["D"], 82.0)       # not final yet: still marked
        self.assertEqual(mids["O"], 32.0)

    def test_a_one_sided_book_is_marked_at_the_last_trade_clamped_to_its_side(self):
        """2026-10-01: Kalshi reads an empty ask as $1.00, and the digest (like
        the bot's bulk mark refresh) averaged it in -- KXDDCOLDBREW-26OCT02-
        T4.45's stray 5c bid under a 97c last trade read 52.5. Now the bot's
        own rule, incentive_mm.bulk_mark_cents."""
        client = _Client(_market("C", bid="0.0500", ask="1.0000", last="0.9700"),
                         _market("B", bid="0.3200", ask="1.0000", last="0.2000"),
                         _market("A", bid="0.0000", ask="0.4000", last="0.5500"),
                         _market("Z", bid="0.0500", ask="1.0000", last="0.0000"),
                         # 50c+ wide (Jack 2026-10-01): the last trade inside
                         # the touch, not the 35.5 mid (KXDKNGAPP-26OCT08-T185)
                         _market("W", bid="0.0100", ask="0.7000", last="0.0100"))
        mids, results = self.sd.current_mids(client, ["C", "B", "A", "Z", "W"])
        self.assertEqual(mids, {"C": 97.0, "B": 32.0, "A": 40.0, "W": 1.0})
        self.assertEqual(results, {})


class RainDirSectionTests(_DigestTest):
    """The rain-directional block's open MTM took (bid + ask) / 2 whenever
    both read nonzero, and Kalshi reads an empty ask as $1.00 (2026-10-01).
    It now marks with the bot's bulk_mark_cents, like current_mids."""

    def test_a_one_sided_book_marks_at_the_last_trade(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old = self.sd.STATUS_PATH
        self.sd.STATUS_PATH = os.path.join(tmp.name, "status_incentive_mm.json")
        self.addCleanup(setattr, self.sd, "STATUS_PATH", old)
        with open(os.path.join(tmp.name, "rain_directional_ledger.csv"), "w",
                  encoding="utf-8", newline="") as f:
            f.write("ts,ticker,take_side,contracts,price_cents,fair_cents,"
                    "ext_bid,ext_ask,edge_cents,order_id\n"
                    "2026-09-30T16:00:00Z,KXRAINNYC-26OCT01,yes,3,40,60,38,40,20,a\n"
                    "2026-09-30T16:00:00Z,KXRAINCHI-26OCT01,no,3,60,30,30,34,10,b\n"
                    "2026-09-29T16:00:00Z,KXRAINDC-26SEP30,yes,3,40,60,38,40,20,c\n")
        client = _Client(_market("KXRAINNYC-26OCT01", bid="0.0500", ask="1.0000", last="0.9700"),
                         _market("KXRAINCHI-26OCT01", bid="0.3000", ask="0.3400", last="0.3100"),
                         _market("KXRAINDC-26SEP30", "yes", "finalized"))
        text, _html = self.sd.rain_dir_section(client)
        # 3 x (97 - 40) + 3 x ((100 - 32) - 60) = 1.71 + 0.24; the stray 5c
        # bid under no offer read 52.5 before, +0.375
        self.assertIn("settled P&L +1.80 | open 2 MTM +1.95 | total +3.75", text[0])


class DashboardWindowsTests(_DigestTest):
    """Jack 2026-10-02: "yesterday RAW will equal the dashboard's total
    trading P&L". The section's yesterday / 7 days / daily table are the
    dashboard's own card totals (imm_dashboard.summary_of), read, not
    re-derived."""

    def setUp(self):
        super().setUp()
        sd = self.sd
        self.mid = lambda m, d: sd.ET.localize(datetime(2026, m, d)).timestamp()
        self.now = sd.ET.localize(datetime(2026, 10, 2, 7, 0)).timestamp()    # the 7:00 send
        self.gen = self.now - 300                                               # its 6:55 build
        days = []
        for i in range(32):                                                     # Sep 1 .. Oct 2
            d = (datetime(2026, 9, 1) + timedelta(days=i)).date()
            ok = d >= datetime(2026, 9, 6).date()
            days.append({"d": d.isoformat(), "ok": ok, "live": d.isoformat() == "2026-10-02",
                         "rew": float(d.day), "pnl": -float(d.day) if ok else None,
                         "fills": 1, "cts": 10.0 * d.day})
        self.summary = {
            "generated": self.gen, "generated_et": "Fri Oct 2, 6:55 AM ET",
            "windows": {
                "yesterday": {"start": self.mid(10, 1), "end": self.mid(10, 2), "pnl_na": False,
                              "rew": 897.48, "pnl": -317.39,
                              "e": {"KXA-1": [500.0, -300.0], "KXB-1": [397.48, -17.39]}},
                "7d": {"start": self.mid(9, 26), "end": self.gen, "pnl_na": False,
                       "rew": 3940.24, "pnl": -1574.46, "e": {"KXA-1": [1.0, -1000.0]}},
                "today": {"start": self.mid(10, 2), "end": self.gen, "pnl_na": False,
                          "rew": 50.0, "pnl": -5.0}},
            "days": days}

    def test_yesterday_and_7_days_are_the_dashboard_cards(self):
        dw = self.sd.dashboard_windows(self.summary, self.now)
        self.assertEqual((dw["day"]["raw"], dw["day"]["reward"]), (-317.39, 897.48))
        self.assertEqual((dw["week"]["raw"], dw["week"]["reward"]), (-1574.46, 3940.24))
        self.assertEqual(dw["week"]["since"], self.mid(9, 26))
        self.assertEqual(dw["day"]["events"], {"KXA-1": -300.0, "KXB-1": -17.39})
        self.assertEqual(dw["note"], "")

    def test_daily_table_is_the_last_30_complete_days_newest_first(self):
        daily = self.sd.dashboard_windows(self.summary, self.now)["daily"]
        self.assertEqual(len(daily), 30)
        self.assertEqual(daily[0][0].isoformat(), "2026-10-01")     # today, still running, is out
        self.assertEqual(daily[-1][0].isoformat(), "2026-09-02")    # Sep 1 is day 31
        self.assertEqual(daily[0][1:], (-1.0, 1.0, 10.0))
        # before the position log: RAW n/a, the rewards still shown
        self.assertEqual(daily[-1][1:], (None, 2.0, 20.0))

    def test_an_older_build_cuts_no_window(self):
        # built 23:55 ET the night before: its "yesterday" is Sep 30
        s = json.loads(json.dumps(self.summary))
        s["generated"] = self.mid(10, 2) - 300
        s["windows"]["yesterday"].update(start=self.mid(9, 30), end=self.mid(10, 1))
        s["windows"]["7d"]["start"] = self.mid(9, 25)
        s["days"] = [dict(r, live=(r["d"] == "2026-10-01")) for r in s["days"]
                     if r["d"] != "2026-10-02"]
        dw = self.sd.dashboard_windows(s, self.now)
        self.assertEqual((dw["day"]["raw"], dw["day"]["reward"], dw["day"]["events"]),
                         (None, None, None))
        self.assertEqual((dw["week"]["raw"], dw["week"]["reward"]), (None, None))
        self.assertIn("NOT REBUILT", dw["note"])
        self.assertEqual(dw["daily"][0][0].isoformat(), "2026-09-30")   # Oct 1 was still running

    def test_a_window_without_marks_keeps_its_rewards(self):
        s = json.loads(json.dumps(self.summary))
        s["windows"]["yesterday"]["pnl_na"] = True
        dw = self.sd.dashboard_windows(s, self.now)
        self.assertIsNone(dw["day"]["raw"])
        self.assertEqual(dw["day"]["reward"], 897.48)
        self.assertIsNone(dw["day"]["events"])

    def test_missing_and_late_summaries_say_so(self):
        dw = self.sd.dashboard_windows({}, self.now)
        self.assertIn("MISSING", dw["note"])
        self.assertEqual(dw["daily"], [])
        self.assertIsNone(dw["day"]["raw"])
        late = dict(self.summary, generated=self.now - 3600)        # built 6:00, still today
        dw = self.sd.dashboard_windows(late, self.now)
        self.assertIn("60 min ago", dw["note"])
        self.assertEqual(dw["day"]["raw"], -317.39)                  # the figures still stand

    def test_the_section_shows_the_dashboard_figures_and_no_events_table(self):
        # Jack 2026-10-02: yesterday RAW = the dashboard's trading P&L, the
        # "Events traded" table gone, the daily table one month long
        from unittest import mock
        sd = self.sd
        now = datetime.fromtimestamp(self.now, timezone.utc)
        stubs = {"build_client": mock.Mock(return_value=object()),
                 "load_dashboard_summary": mock.Mock(return_value=self.summary),
                 "load_json": mock.Mock(return_value={}),
                 "current_mids": mock.Mock(return_value=({}, {})),
                 "event_rows": mock.Mock(return_value=([], {}, {})),
                 "risk_section": mock.Mock(return_value=(
                     sd.risk._empty_evidence(self.now, self.now), [])),
                 "capacity_note": mock.Mock(return_value=""),
                 "cutoff_audit": mock.Mock(return_value={}),
                 "cutoff_banner": mock.Mock(return_value=""),
                 "_cutoff_audit_text": mock.Mock(return_value=[]),
                 "_cutoff_audit_html": mock.Mock(return_value=""),
                 "_calibration_caveat_text": mock.Mock(return_value=[]),
                 "_calibration_caveat_html": mock.Mock(return_value=""),
                 "health_line": mock.Mock(return_value="Bot: alive")}
        pick = {"kalshi": {}, "rows": []}
        with mock.patch.multiple(sd, **stubs), \
                mock.patch.object(sd.imm_pickoff, "scan", return_value=pick), \
                mock.patch.object(sd.imm_pickoff, "text_lines", return_value=[]), \
                mock.patch.object(sd.imm_pickoff, "html_block", return_value=""), \
                mock.patch.object(sd.imm_pickoff, "error_text", return_value=""):
            text, html, risk_text, risk_html = sd.build_digest(now)
        row = "{:10s} {:>11s} {:>11s} {:>11s}"
        self.assertIn(row.format("yesterday", "-317.39", "+897.48", "+580.09"), text)
        self.assertIn(row.format("7 days", "-1,574.46", "+3,940.24", "+2,365.78"), text)
        self.assertNotIn("EVENTS TRADED", text)
        self.assertNotIn("Events traded", html)
        # Jack 2026-10-03: the risk block rides on its own (the portfolio
        # email puts it under its chart); finecon / open scan are gone
        self.assertNotIn("RISK CONTROLS", text)
        self.assertIn("RISK CONTROLS", risk_text)
        self.assertIn("Risk controls", risk_html)
        for gone in ("FINECON", "OPEN SCAN", "Finecon sweep", "Open scan"):
            self.assertNotIn(gone, text)
            self.assertNotIn(gone, html)
        dated = [ln for ln in text.splitlines() if ln[:5] == "2026-"]
        self.assertEqual(len(dated), 30)
        self.assertTrue(dated[0].startswith("2026-10-01"))
        self.assertIn("-317.39", html)
        # TOTAL over the 26 days with a RAW: Sep 6 .. Oct 1
        n = [d for d in range(6, 31)] + [1]
        self.assertIn("{:12s} {:>11s}".format("TOTAL", "{:+,.2f}".format(-sum(n))), text)
        self.assertIn("TOTAL = the 26 of 30 days with a RAW", text)
        # the health line hears about a late dashboard
        self.assertEqual(stubs["health_line"].call_args[0][2], "")


class SectionTests(_DigestTest):
    """Jack 2026-10-02: "cut it as a standalone email and add it into the
    Kalshi portfolio ... email"."""

    def test_the_old_7_10_task_run_sends_nothing(self):
        from unittest import mock
        with mock.patch.object(self.sd, "build_digest", side_effect=AssertionError("built")), \
                mock.patch.object(self.sd, "Alerter", side_effect=AssertionError("sent")):
            self.assertEqual(self.sd.main([]), 0)

    def test_section_out_writes_the_section_and_its_subject_flag(self):
        from unittest import mock
        body = ">> " + self.sd.imm_pickoff.HEADER + ": KXFOO\nrest"
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(self.sd, "build_digest",
                                  return_value=(body, "<div>x</div>", "RISK", "<div>r</div>")):
            p = os.path.join(d, "s.json")
            self.assertEqual(self.sd.main(["--section-out", p]), 0)
            with open(p, encoding="utf-8") as f:
                sec = json.load(f)
        self.assertEqual((sec["text"], sec["html"]), (body, "<div>x</div>"))
        # the risk block travels on its own, for the portfolio email's chart
        self.assertEqual((sec["risk_text"], sec["risk_html"]), ("RISK", "<div>r</div>"))
        self.assertEqual(sec["subject_flag"], " - " + self.sd.imm_pickoff.HEADER)
        self.assertEqual(self.sd.subject_flag("no window today"), "")


if __name__ == "__main__":
    unittest.main()
