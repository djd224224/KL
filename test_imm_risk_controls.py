"""Tests for imm_risk_controls (the 7:00 email's risk-controls block) and the
bot's "risk:" log line it reads."""
import os
import unittest
from datetime import datetime, timezone

import imm_risk_controls as rc

T0 = datetime(2026, 10, 3, 11, 0, tzinfo=timezone.utc).timestamp()   # 07:00 ET
SINCE = T0 - 24 * 3600


def _line(ts_utc: str, msg: str) -> str:
    return f"{ts_utc}Z [IMM] {msg}\n"


def _ctx(**now):
    caps = {"ACCOUNT_DROP_HALT": 2000.0, "DAILY_LOSS_LIMIT": 1200.0,
            "SCAN_DAILY_LOSS_LIMIT": 200.0, "SCAN_TOP_N": 60, "FINECON_TOP_N": 25,
            "FAILSAFE_CANCEL_AFTER": 4, "MAX_MARKETS": 1000, "COLLATERAL_BUDGET": 100000.0,
            "MAX_CANDIDATE_BOOKS": 5000, "MAX_TOTAL_RESTING_ORDERS": 4000,
            "MAX_PLACEMENTS_PER_CYCLE": 1000, "PLACE_RATE_PER_SEC": 12.0,
            "TOXIC_HALT": True, "TOXIC_PICKOFFS": 2, "TOXIC_HALT_MIN": 30.0,
            "BREAKERS_ENABLED": False, "EVENT_FILL_HALT_CONTRACTS": 15.0,
            "SCAN_FILL_HALT_CONTRACTS": 0.0, "SCAN_MID_JUMP_CENTS": 0.0, "SCAN_DRIFT_CENTS": 0.0}
    base = {"pnl_today": 0.0, "scan_pnl": 0.0, "scan_halted": False, "errors_today": 0,
            "halt_file": False, "manual_standoff": [], "acct_value": None,
            "acct_anchor": None, "resting_orders": None, "resting_notional": None,
            "positions": [], "events": []}
    base.update(now)
    return {"caps": caps, "now": base}


def _ev(lines=(), toxic=(), guards=()):
    ev = rc._empty_evidence(SINCE, T0)
    rc.scan_log_lines(lines, ev)
    rc.scan_toxic(toxic, ev)
    rc.scan_guards(guards, ev)
    return ev


def _by(rows):
    return {r["name"]: r for r in rows}


UNIVERSE = ("universe: 8714 program markets -> 4374 candidates -> 1861 selected across "
            "392/1000 events (0 forced quote-all @ ~$199849, total ~$199849 ladder "
            "collateral, $24309 inventory reserve); skips {'cutoff': 996, 'manual': 36, "
            "'event_top_n': 28, 'finecon_top_n': 3, 'scan_top_n': 8, 'budget': {budget}}")

# 10/3 01:40 ET: the CASH guard's real trip and the idle lines that followed
CASH_TRIP = [
    _line("2026-10-03 05:35:00", "1790/1865 mkts quoted, 3428 resting, 102 amend, 968 cancel, "
          "981 place, est $4065.05/day reward share, P&L today $-594.40 (real -10.58/unreal "
          "-3930.17) "),
    _line("2026-10-03 05:40:54", "ALERT [balance_floor] ACCOUNT balance dropped $5097 since "
          "the daily anchor ($8978 -> $3882) >= $5000; cancelled 3590 IMM orders and halted "
          "until the 5am CT roll. NOTE: the account is shared"),
    _line("2026-10-03 05:41:08", "daily-loss halt active until 2026-10-03 10:00:00+00:00; idle"),
    _line("2026-10-03 09:59:57", "daily-loss halt active until 2026-10-03 10:00:00+00:00; idle"),
]


class LogScanTests(unittest.TestCase):

    def test_reads_alerts_cycles_universe_and_caps(self):
        lines = CASH_TRIP + [
            _line("2026-10-03 06:10:00", UNIVERSE.replace("{budget}", "87")),
            _line("2026-10-03 06:11:00", "placement cap 1000/cycle reached; 332 deferred to "
                  "next cycle"),
            _line("2026-10-03 06:11:01", "placement cap: 227 identical renewal(s) left "
                  "resting until the next cycle"),
            _line("2026-10-03 06:12:00", "1/2 mkts quoted, 3999 resting, 0 amend, 0 cancel, "
                  "0 place, est $1.00/day reward share, P&L today $+5.00 (real 0/unreal 5) "
                  "[fast] "),
            _line("2026-10-03 06:13:00", "! cycle error #2: ReadTimeout()"),
        ]
        ev = _ev(lines)
        self.assertEqual(len(ev["alerts"]["balance_floor"]), 1)
        self.assertEqual(ev["pnl_min"][0], -594.40)
        self.assertEqual(ev["resting_max"][0], 3999)       # a fast tick still rests orders
        self.assertEqual(ev["cycles"], 1)                   # ...but is not a full cycle
        self.assertEqual(ev["universe"][0]["skips"]["budget"], 87)
        self.assertEqual(ev["universe"][0]["reserved"], 199849 + 24309)
        self.assertEqual(ev["place_cap"], [(ev["place_cap"][0][0], 1000, 332)])
        self.assertEqual(ev["renewals_kept"], 227)
        self.assertEqual(ev["cycle_errors_max"], 2)
        self.assertEqual(len(ev["idle"]), 1)

    def test_launcher_lines_and_lines_outside_the_window_are_ignored(self):
        lines = [
            # the launcher writes LOCAL time with a fake Z and no [IMM] tag
            "2026-10-03 01:40:00Z launcher: bot exited (code 0); restarting in 30s\n",
            _line("2026-10-02 10:59:59", "ALERT [loss_halt] P&L today $-1300 <= -$1200; "
                  "cancelled 9 orders"),                     # a second before the window
            _line("2026-10-03 11:00:01", "ALERT [loss_halt] later"),   # after it
        ]
        ev = _ev(lines)
        self.assertEqual(ev["lines"], 0)
        self.assertFalse(ev["alerts"])

    def test_risk_lines_give_the_worst_account_drop_and_scan_pnl(self):
        lines = [
            _line("2026-10-03 07:00:00", "risk: account value $25,000 (anchor $26,000, down "
                  "$1,000 of $2,000 halt) | P&L today $-50.00 of -$1,200 halt | open-scan "
                  "$-20.00 of -$200 budget"),
            _line("2026-10-03 08:00:00", "risk: account value $24,400 (anchor $26,000, down "
                  "$1,600 of $2,000 halt) | P&L today $-80.00 of -$1,200 halt | open-scan "
                  "$-150.00 of -$200 budget"),
            _line("2026-10-03 09:00:00", "risk: account value $26,500 (anchor $26,000, up $500 "
                  "of $2,000 halt) | P&L today $+10.00 of -$1,200 halt"),
        ]
        ev = _ev(lines)
        self.assertEqual(ev["acct_worst"][0], 1600.0)
        self.assertEqual(ev["scan_min"][0], -150.0)
        rows = _by(rc.build_rows(ev, _ctx()))
        self.assertEqual(rows["Account-value guard (balance guard)"]["status"], "CLOSE")  # 80%
        self.assertEqual(rows["Open-scan tier loss budget"]["status"], "SLACK")           # 75%


class VerdictTests(unittest.TestCase):

    def test_the_10_3_cash_trip_reads_tripped_with_what_it_did(self):
        rows = _by(rc.build_rows(_ev(CASH_TRIP), _ctx(acct_value=27861.89,
                                                      acct_anchor=24387.0)))
        g = rows["Account-value guard (balance guard)"]
        self.assertEqual(g["status"], "TRIPPED")
        self.assertIn("CASH (the old rule)", g["impact"])
        self.assertIn("$5,097", g["impact"])
        self.assertIn("3,590 orders cancelled", g["impact"])
        self.assertIn("4.3h", g["impact"])                  # 05:40:54Z -> the 10:00Z roll
        self.assertEqual(g["now"], "up $3,475")
        # the loss halt saw -$594 that night: half its limit, not constraining
        self.assertEqual(rows["Daily loss halt"]["status"], "SLACK")
        self.assertIn("TRIPPED: Account-value guard", rc.headline(list(rows.values())))

    def test_a_loss_counts_against_the_halt_and_a_gain_does_not(self):
        lines = [_line("2026-10-03 07:00:00", "1/2 mkts quoted, 10 resting, 0 amend, 0 cancel, "
                       "0 place, est $1.00/day reward share, P&L today $-1000.00 (r/u) ")]
        rows = _by(rc.build_rows(_ev(lines), _ctx(pnl_today=+900.0)))
        self.assertEqual(rows["Daily loss halt"]["status"], "CLOSE")     # 1000 of 1200
        self.assertAlmostEqual(rows["Daily loss halt"]["pct"], 1000 / 1200)
        lines = [_line("2026-10-03 07:00:00", "1/2 mkts quoted, 10 resting, 0 amend, 0 cancel, "
                       "0 place, est $1.00/day reward share, P&L today $+900.00 (r/u) ")]
        rows = _by(rc.build_rows(_ev(lines), _ctx()))
        self.assertEqual(rows["Daily loss halt"]["status"], "SLACK")
        self.assertEqual(rows["Daily loss halt"]["pct"], 0.0)

    def test_budget_and_placements_bind_when_they_refused_or_deferred(self):
        lines = [_line("2026-10-03 06:10:00", UNIVERSE.replace("{budget}", "87")),
                 _line("2026-10-03 06:11:00", "placement cap 1000/cycle reached; 332 deferred "
                       "to next cycle")]
        rows = _by(rc.build_rows(_ev(lines), _ctx()))
        self.assertEqual(rows["Collateral budget (modelled)"]["status"], "BINDING")
        self.assertIn("87 new markets refused", rows["Collateral budget (modelled)"]["impact"])
        self.assertEqual(rows["Placements per cycle"]["status"], "BINDING")
        self.assertEqual(rows["Slot caps (strikes per event, scan, finecon)"]["status"],
                         "BINDING")
        self.assertEqual(rows["Events quoted"]["status"], "SLACK")         # 392 of 1000
        # a refresh that refused nothing and no placement-cap line: not binding
        rows = _by(rc.build_rows(_ev([_line("2026-10-03 06:10:00",
                                            UNIVERSE.replace("{budget}", "0")
                                            .replace("'event_top_n': 28, 'finecon_top_n': 3, "
                                                     "'scan_top_n': 8, ", ""))]), _ctx()))
        self.assertNotEqual(rows["Collateral budget (modelled)"]["status"], "BINDING")
        self.assertEqual(rows["Placements per cycle"]["status"], "SLACK")

    def test_resting_orders_close_then_binding_on_the_cap_alert(self):
        lines = [_line("2026-10-03 07:00:00", "1/2 mkts quoted, 3743 resting, 0 amend, 0 cancel, "
                       "0 place, est $1.00/day reward share, P&L today $+0.00 (r/u) ")]
        rows = _by(rc.build_rows(_ev(lines), _ctx(resting_orders=3010)))
        self.assertEqual(rows["Resting orders"]["status"], "CLOSE")
        self.assertEqual(rows["Resting orders"]["worst"], "3,743")
        lines.append(_line("2026-10-03 07:05:00", "ALERT [order_cap] global resting-order cap "
                           "4000 reached"))
        rows = _by(rc.build_rows(_ev(lines), _ctx(resting_orders=3010)))
        self.assertEqual(rows["Resting orders"]["status"], "BINDING")

    def test_positions_are_judged_against_their_own_family_cap(self):
        """The 10/3 false alarm: a 400-lot NFL ladder read "267% AT CAP" against the
        global 150 while its family cap is 750."""
        now = {"positions": [("KXNFLLADDERRECYDS-26OCT04MIAMIN-MINAJONES33", -400.0, 750.0),
                             ("KXANFCC-26OCT07-T98", 125.0, 150.0)],
               "events": [("KXTRUMPMENTION-26OCT02", 616.0, 1000.0)]}
        rows = _by(rc.build_rows(_ev(), _ctx(**now)))
        pm = rows["Per-market position cap"]
        self.assertEqual(pm["status"], "CLOSE")                # 125/150 is the worst, 83%
        self.assertIn("KXANFCC", pm["impact"])
        self.assertEqual(rows["Per-event net cap"]["status"], "SLACK")
        now["positions"].append(("KXTEMPAUSH-26AUG0409-T79.99", -70.0, 50.0))
        pm = _by(rc.build_rows(_ev(), _ctx(**now)))["Per-market position cap"]
        self.assertEqual(pm["status"], "BINDING")
        self.assertIn("1 OVER it", pm["impact"])
        self.assertIn("KXTEMPAUSH", pm["impact"])

    def test_guards_fire_count_or_say_off(self):
        toxic = [{"kind": "pickoff", "ts": T0 - 100, "ticker": "A"},
                 {"kind": "side_halt", "ts": T0 - 90, "until": T0 + 1710, "ticker": "A",
                  "est_per_day": 48.0},
                 {"kind": "event_halt", "ts": T0 - 30 * 3600, "until": T0, "event": "OLD"}]
        guards = [{"kind": "enter", "ts": "2026-10-03T08:00:00.000+00:00", "guard":
                   "treasury_yield", "ticker": "KXUST2AM-26OCT30-T5.01"},
                  {"kind": "enter", "ts": "2026-10-03T08:00:00.000+00:00", "guard":
                   "cpi_blackout", "ticker": "KXCPI-26OCT-T0.3"},
                  {"kind": "clear", "ts": "2026-10-03T08:05:00.000+00:00", "guard":
                   "cpi_blackout", "ticker": "KXCPI-26OCT-T0.3"}]
        rows = _by(rc.build_rows(_ev(toxic=toxic, guards=guards), _ctx()))
        tx = rows["Toxic-flow halts"]
        self.assertEqual(tx["status"], "FIRED")
        self.assertIn("1 side + 0 event halts", tx["impact"])        # the old one is outside
        self.assertIn("~$1", tx["impact"])                            # 48/day x 30 min
        self.assertEqual(rows["Fair-value & data gates"]["status"], "FIRED")
        self.assertEqual(rows["Release blackouts (CPI, AAA)"]["status"], "FIRED")
        self.assertEqual(rows["Circuit breakers (mid move / fill burst / one-sided)"]["status"],
                         "OFF")
        self.assertEqual(rows["Watchdog & coverage alerts"]["status"], "QUIET")

    def test_rows_sort_worst_first_with_the_guards_after(self):
        rows = rc.build_rows(_ev(CASH_TRIP), _ctx())
        kinds = [r["kind"] for r in rows]
        self.assertEqual(kinds.index("GUARD"), len([k for k in kinds if k != "GUARD"]))
        self.assertEqual(rows[0]["status"], "TRIPPED")
        order = [rc.STATUS_ORDER.index(r["status"]) for r in rows if r["kind"] != "GUARD"]
        self.assertEqual(order, sorted(order))

    def test_renders_text_and_html(self):
        rows = rc.build_rows(_ev(CASH_TRIP), _ctx())
        text = "\n".join(rc.text_lines(rows, _ev(CASH_TRIP), timezone.utc, "caps ok"))
        self.assertIn("!!TRIPPED Account-value guard (balance guard)", text)
        self.assertIn("SLACK   Daily loss halt", text)
        html = rc.html(rows, _ev(CASH_TRIP), timezone.utc, "caps ok", "td", "tdl")
        self.assertIn("TRIPPED in the last 24h", html)
        self.assertNotIn("—", html)                # entities only in the email html
        self.assertIn("Per-market guards", html)

    def test_an_empty_window_says_it_is_blind(self):
        ev = _ev()
        self.assertIn("blind", rc.window_note(ev, timezone.utc))
        rows = rc.build_rows(ev, _ctx())
        self.assertTrue(rows)                            # still one row per control


class BotRiskLineContractTests(unittest.TestCase):
    """incentive_mm.risk_line writes what imm_risk_controls reads: change one,
    this fails."""

    @classmethod
    def setUpClass(cls):
        import incentive_mm
        cls.imm = incentive_mm

    def test_the_bot_line_parses(self):
        line = self.imm.risk_line((24412.5, 26000.0), -80.0, -150.0)
        ev = _ev([_line("2026-10-03 08:00:00", line)])
        self.assertEqual(ev["acct_worst"][0], 1588.0)          # 26,000 - 24,412 (rounded)
        if self.imm.SCAN_TOP_N > 0 and self.imm.SCAN_DAILY_LOSS_LIMIT > 0:
            self.assertEqual(ev["scan_min"][0], -150.0)
        self.assertRegex(line, rc.RISK_PNL_RE)

    def test_an_account_up_on_the_day_reads_up(self):
        line = self.imm.risk_line((26500.0, 26000.0), 10.0, None)
        self.assertIn("up $500", line)
        ev = _ev([_line("2026-10-03 08:00:00", line)])
        self.assertEqual(ev["acct_worst"][0], -500.0)
        self.assertNotIn("open-scan", line)

    def test_no_reading_leaves_the_account_part_out(self):
        line = self.imm.risk_line(None, -5.0, None)
        self.assertTrue(line.startswith("risk: P&L today $-5.00"))


if __name__ == "__main__":
    unittest.main()
