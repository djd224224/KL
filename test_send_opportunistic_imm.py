#!/usr/bin/env python3
"""Unit tests for send_opportunistic_imm.py's new-since-last-email marking
(Jack 2026-09-13: "always bold the events that are new (werent in the
previous email)") and for the SCAN PERF block + THE COST LINE.
Run: python -m unittest test_send_opportunistic_imm"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import send_imm_digest as sd
import send_opportunistic_imm as so


LOG = """2026-09-12 11:25:01Z opportunistic body:
Opportunistic IMM - 2026-09-12

FINECON - curated
22/25 slots, +0/10 daily openings used.
EVENT                      WHAT IT IS                    QUOT HELD  PERIOD$  ALL-TIME$    CRED$      P&L$      NET$
OLDONLY-26SEP30            an event only in the old one     3             -      26.41    18.77     -3.90    +22.51
TOTAL                                                      22    3        -      81.45    53.16    -40.40    +41.05
2026-09-12 11:25:02Z opportunistic send: ok
2026-09-13 11:25:08Z opportunistic body:
Opportunistic IMM - 2026-09-13

FINECON - curated Finance/Economics quiet prints
22/25 slots, +0/10 daily openings used.
EVENT                      WHAT IT IS                    QUOT HELD  PERIOD$  ALL-TIME$    CRED$      P&L$      NET$
AAAGASMINM-26SEP30         Monthly low US gas price         3             -      26.41    18.77     -3.90    +22.51
CBDECISIONNZ-26OCT27       RBNZ rate decision               0    1        -       6.33    10.92     +5.40    +11.73
TOTAL                                                      22    3        -      81.45    53.16    -40.40    +41.05

OPEN SCAN - all other markets
35/30 slots, +0/5 daily openings used.
EVENT                      WHAT IT IS                    QUOT HELD  PERIOD$  ALL-TIME$    CRED$      P&L$      NET$
BABELMANDEBWEEKLY-26SEP20  Traffic through the Bab el-M     1          4.41       9.23              +0.00     +9.23
TOTAL                                                       1             -       9.23              +0.00     +9.23

CUMULATIVE - CREDITED and REALIZED cover every event
TIER             EVENTS  CREDITED$      EST$  REALIZED$      MTM$      NET$
FINECON              47     162.10     81.45     -12.30    -28.10    +41.05
2026-09-13 11:25:09Z opportunistic send: ok
2026-09-13 14:50:00Z opportunistic body:
Opportunistic IMM - 2026-09-13 (a --dry run: never sent, must not count)
EVENT                      WHAT IT IS                    QUOT HELD  PERIOD$  ALL-TIME$    CRED$      P&L$      NET$
DRYONLY-26SEP30            only in the dry run              1             -       1.00              +0.00     +1.00
TOTAL                                                       1             -       1.00              +0.00     +1.00
"""


def _row(ev, net=1.0):
    return {"event": ev, "label": "what it is", "tier": "scan", "mkts": 1,
            "held": 0, "earn": 1.0, "period": None, "cred": 0.0,
            "pnl": 0.0, "net": net, "pos": 0}


class TestParseLastSentBody(unittest.TestCase):
    def test_last_SENT_body_wins_and_a_dry_body_does_not_count(self):
        ts, evs = so.parse_last_sent_body(LOG)
        self.assertEqual(ts, "2026-09-13 11:25:08Z")
        self.assertEqual(evs, {"AAAGASMINM-26SEP30", "CBDECISIONNZ-26OCT27",
                               "BABELMANDEBWEEKLY-26SEP20"})
        self.assertNotIn("DRYONLY-26SEP30", evs)      # dry body, never sent
        self.assertNotIn("OLDONLY-26SEP30", evs)      # the email before last
        self.assertNotIn("FINECON", evs)              # cumulative TIER rows

    def test_no_sent_body(self):
        self.assertEqual(so.parse_last_sent_body(""), (None, None))
        self.assertEqual(so.parse_last_sent_body(
            "2026-09-13 14:50:00Z opportunistic body:\nEVENT   x\nA-1  y\n"),
            (None, None))


class TestPreviousEmailEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opp_test_")
        self._old = (so.LAST_SENT_PATH, so.TASK_LOG_PATH)
        so.LAST_SENT_PATH = os.path.join(self.tmp, "last.json")
        so.TASK_LOG_PATH = os.path.join(self.tmp, "task.log")

    def tearDown(self):
        so.LAST_SENT_PATH, so.TASK_LOG_PATH = self._old

    def test_none_when_nothing_exists(self):
        self.assertEqual(so.previous_email_events(), (None, None, "none"))

    def test_log_fallback_then_record_precedence(self):
        with open(so.TASK_LOG_PATH, "w", encoding="utf-8") as f:
            f.write(LOG)
        ts, evs, src = so.previous_email_events()
        self.assertEqual((src, ts), ("log", "2026-09-13 11:25:08Z"))
        self.assertIn("AAAGASMINM-26SEP30", evs)
        # a record written by a send wins over the log, and is stored as
        # FULL event tickers but compared as short names
        now = datetime(2026, 9, 13, 15, 0, tzinfo=timezone.utc)
        so.write_last_sent(now, ["KXNEWREC-26OCT01", "KXOTHER-26OCT01"])
        ts2, evs2, src2 = so.previous_email_events()
        self.assertEqual(src2, "record")
        self.assertEqual(evs2, {"NEWREC-26OCT01", "OTHER-26OCT01"})
        self.assertTrue(ts2.startswith("2026-09-13T15:00"))
        with open(so.LAST_SENT_PATH, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["events"],
                             ["KXNEWREC-26OCT01", "KXOTHER-26OCT01"])


class TestRenderingMarksOnlyNewRows(unittest.TestCase):
    ROWS = [_row("KXNEWONE-26OCT01", 2.0), _row("KXOLDONE-26OCT01", 1.0)]

    def test_html_bolds_only_the_new_row(self):
        h = so.html_table(self.ROWS, new_events={"KXNEWONE-26OCT01"})
        self.assertIn("<b>NEWONE-26OCT01</b>", h)
        self.assertIn("NEW</span>", h)
        self.assertNotIn("<b>OLDONE-26OCT01</b>", h)
        self.assertIn(">OLDONE-26OCT01<", h)
        # nothing new -> nothing bold in the EVENT column at all
        h0 = so.html_table(self.ROWS)
        self.assertNotIn("<b>NEWONE", h0)
        self.assertNotIn("NEW</span>", h0)

    def test_text_twin_marks_with_an_asterisk(self):
        L = so.text_table(self.ROWS, new_events={"KXNEWONE-26OCT01"})
        body = "\n".join(L)
        self.assertIn("*NEWONE-26OCT01", body)
        self.assertNotIn("*OLDONE", body)
        # the 27-character EVENT column is preserved with the marker
        row = [l for l in L if "NEWONE" in l][0]
        self.assertEqual(row[:27].strip(), "*NEWONE-26OCT01")


def _table(age_h=4.0, **over):
    """A scan_perf.json shaped exactly like the spec's schema, small enough to
    read in the assertions below. The numbers are the Phase-1 MEASURED ones so
    a mislabelling shows up as a wrong figure, not as a plausible one."""
    gen = (datetime.now(timezone.utc)
           - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t = {
        "version": 1,
        "generated_at": gen,
        "params": {"score_basis": "trading", "dim_clip": 0.04,
                   "max_req": 0.10, "edges_refit": False},
        "coverage": {"markets": 147, "events": 83, "series": 71,
                     "fills": 106, "fills_unmarked": 0, "episodes": 85,
                     "episodes_matured": 63, "risk_days": 10434.2,
                     "credited_events": 2},
        "tier": {"markout_c_per_ct_24h": -5.09,
                 "rent_modelled_dollars": 244.33, "rent_basis": "est_floored",
                 "mu_trading_only": -0.02279, "mu_rent_blended": 0.00063,
                 "credited_measured_dollars": 21.28,
                 "mark_source_mix": {"two_sided": 0.71, "one_sided": 0.21,
                                     "settlement": 0.08}},
        "cohorts": {"spread": {"field": "spread_cents", "buckets": [
            {"key": "1c", "lo": 0, "hi": 1, "centre": 1.0, "n_markets": 51,
             "n_series": 24, "n_events": 30, "risk_days": 5848.3,
             "raw_trading": 0.0068, "dev_trading": 0.0058,
             "raw_blend": 0.0121, "dev_blend": 0.0106, "muted": False},
            {"key": "5-9c", "lo": 5, "hi": 9, "centre": 7.0, "n_markets": 31,
             "n_series": 18, "n_events": 21, "risk_days": 1835.1,
             "raw_trading": -0.0204, "dev_trading": -0.0173,
             "raw_blend": -0.0102, "dev_blend": -0.0088, "muted": True}]}},
        "series": {"KXCPIYOY": {
            "n_episodes": 3, "n_markets": 3, "contracts_matured": 150,
            "markout_c_per_ct_24h": -25.5, "rent_modelled": 0.0,
            "dev": -0.0129, "verdict": "down_rank", "credited_measured": 0.0}},
        # The scorer keys `events` by EVENT ROOT (imm_scan_perf.root_of ==
        # incentive_mm.scan_perf_event_root), never by the dated event.
        "events": {"KXEOWEEK": {
            "root": "KXEOWEEK", "n_episodes": 8, "n_markets": 2,
            "contracts_matured": 40, "markout_c_per_ct_24h": -11.4,
            "rent_modelled": 2.00, "dev": -0.0402, "verdict": "bar",
            "until": "2026-10-02T11:55:04Z", "credited_measured": 4.61}},
        "limits": {"down_ranked": 1, "barred_series": 0, "barred_events": 1},
        "unmatched_bar_keys": ["series:KXGONE"],
        "warnings": ["credit ledger 8 days stale (newest 2026-09-11)"],
    }
    t.update(over)
    return t


ZERO_COST = {"window_h": 24, "seats_empty": 0, "barred": 0, "floored_out": 0,
             "forgone_dollars_per_day": 0.0, "seats_cap": 60}
STATUS = {"scan_perf": {"weight": 0.0, "bar_enabled": False}}


class TestScanPerfBlock(unittest.TestCase):
    """Jack 2026-09-06: "a model is not a measurement". The block exists to
    put the MEASURED markout, the MODELLED rent and the MEASURED credits in
    front of a human WITHOUT ever adding the last two together, and to state
    the WEIGHT and BAR in force so a table of intentions cannot be read as a
    table of actions."""

    def test_block_renders_from_a_fixture_table(self):
        t = _table()
        hdr = sd.scan_perf_header(t, STATUS)
        # the previous email saw the same verdicts, so nothing is marked
        # changed here (an EMPTY prev dict means "a record exists and had no
        # non-neutral rows", which correctly marks every row as changed)
        body = "\n".join(so.scan_perf_text_block(
            t, hdr, ZERO_COST, so.perf_verdict_map(t)))
        self.assertIn("WEIGHT=0.0", body)
        self.assertIn("BAR off", body)
        self.assertIn("bot status file", body)      # says WHICH source
        self.assertNotIn("STALE", body)
        # cohorts, BOTH bases, with the acting column marked
        self.assertIn("DEV_TR*", body)
        self.assertIn("DEV_BL", body)
        self.assertNotIn("DEV_BL*", body)
        self.assertIn("+0.0058", body)              # dev_trading
        self.assertIn("+0.0106", body)              # dev_blend
        self.assertIn("muted", body)
        # non-neutral keys, both units
        self.assertIn("KXCPIYOY", body)
        self.assertIn("KXEOWEEK", body)          # keyed by EVENT ROOT
        self.assertIn("2026-10-02", body)           # the bar's until
        self.assertIn("ACTIVE BARS", body)
        self.assertIn("series:KXGONE", body)        # unmatched_bar_keys
        # mark source mix + unmarked fraction
        self.assertIn("two_sided 0.71", body)
        self.assertIn("unmarked 0.00", body)
        self.assertIn("credit ledger 8 days stale", body)

    def test_measured_credits_are_a_separate_column_never_added_to_est(self):
        """The accrued_est/credited trap, one level down: rent here is
        MODELLED est and credited is MEASURED money, and 244.33 + 21.28 must
        appear NOWHERE."""
        t = _table()
        hdr = sd.scan_perf_header(t, STATUS)
        body = "\n".join(so.scan_perf_text_block(
            t, hdr, ZERO_COST, so.perf_verdict_map(t)))
        self.assertIn("244.33", body)
        self.assertIn("21.28", body)
        self.assertNotIn("265.61", body)
        self.assertIn("never", body.split("MEASURED credits")[1][:200])
        # and the per-row credited figure is its own column
        row = [l for l in body.splitlines() if l.startswith("KXEOWEEK")][0]
        self.assertTrue(row.rstrip().endswith("4.61"), row)

    def test_stale_table_shows_the_banner_and_applies_nothing(self):
        t = _table(age_h=72.0)
        hdr = sd.scan_perf_header(t, STATUS)
        self.assertTrue(hdr["stale"])
        body = "\n".join(so.scan_perf_text_block(
            t, hdr, ZERO_COST, so.perf_verdict_map(t)))
        self.assertIn("STALE — verdicts not applied", body)
        # a stale table's req is shown as a would-be value, never as applied
        self.assertIn("(0.0129)", body)
        html = so.scan_perf_html_block(t, hdr, ZERO_COST,
                                       so.perf_verdict_map(t))
        self.assertEqual(html.count("STALE"), 1, "stale banner rendered twice")

    def test_missing_table_renders_no_table_and_raises_nothing(self):
        """The email must never fail because of this block."""
        self.assertEqual(sd.load_scan_perf(os.path.join(
            tempfile.gettempdir(), "definitely_not_a_table_12345.json")), {})
        hdr = sd.scan_perf_header({}, None)
        self.assertFalse(hdr["present"])
        body = "\n".join(so.scan_perf_text_block({}, hdr, ZERO_COST, None))
        self.assertIn("no table", body)
        self.assertIn("seats left empty by perf_roi", body)
        html = so.scan_perf_html_block({}, hdr, ZERO_COST, None)
        self.assertIn("no table", html)

    def test_bad_json_and_a_non_object_are_no_table_too(self):
        tmp = tempfile.mkdtemp(prefix="perf_test_")
        self.addCleanup(shutil.rmtree, tmp, True)
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(sd.load_scan_perf(bad), {})
        arr = os.path.join(tmp, "arr.json")
        with open(arr, "w", encoding="utf-8") as f:
            f.write("[1,2,3]")
        self.assertEqual(sd.load_scan_perf(arr), {})
        nogen = os.path.join(tmp, "nogen.json")
        with open(nogen, "w", encoding="utf-8") as f:
            json.dump({"version": 1}, f)
        self.assertEqual(sd.load_scan_perf(nogen), {})

    def test_a_changed_verdict_is_marked_and_bolded(self):
        t = _table()
        hdr = sd.scan_perf_header(t, STATUS)
        prev = {"series:KXCPIYOY": "neutral", "event:KXEOWEEK": "bar"}
        body = "\n".join(so.scan_perf_text_block(t, hdr, ZERO_COST, prev))
        self.assertIn("!KXCPIYOY", body)          # neutral -> down_rank
        self.assertNotIn("!KXEOWEEK", body)       # unchanged
        html = so.scan_perf_html_block(t, hdr, ZERO_COST, prev)
        self.assertIn("<b>!KXCPIYOY", html)
        # no record at all -> nothing is marked, and the block says so
        body0 = "\n".join(so.scan_perf_text_block(t, hdr, ZERO_COST, None))
        self.assertNotIn("!KXCPIYOY", body0)
        self.assertIn("no previous SCAN PERF record", body0)

    def test_the_verdict_record_round_trips_through_last_sent(self):
        tmp = tempfile.mkdtemp(prefix="perf_test_")
        self.addCleanup(shutil.rmtree, tmp, True)
        old = so.LAST_SENT_PATH
        so.LAST_SENT_PATH = os.path.join(tmp, "last.json")
        self.addCleanup(lambda: setattr(so, "LAST_SENT_PATH", old))
        self.assertIsNone(so.previous_perf_verdicts())
        v = so.perf_verdict_map(_table())
        self.assertEqual(v, {"series:KXCPIYOY": "down_rank",
                             "event:KXEOWEEK": "bar"})
        so.write_last_sent(datetime.now(timezone.utc), ["KXA-26OCT01"], v)
        self.assertEqual(so.previous_perf_verdicts(), v)

    def test_perf_cell_is_bar_or_a_req_and_never_promotes(self):
        t = _table()
        hdr_off = sd.scan_perf_header(t, STATUS)
        hdr_on = sd.scan_perf_header(
            t, {"scan_perf": {"weight": 1.0, "bar_enabled": True}})
        _k, unit, rec = so.perf_record(t, "KXEOWEEK-26SEP19")
        self.assertEqual(unit, "event")
        self.assertEqual(so.perf_cell(rec, hdr_off), "(BAR)")   # bar disarmed
        self.assertEqual(so.perf_cell(rec, hdr_on), "BAR")
        _k, unit, rec = so.perf_record(t, "KXCPIYOY-26NOV")
        self.assertEqual(unit, "series")                        # root fallback
        self.assertEqual(so.perf_cell(rec, hdr_off), "(0.013)")
        self.assertEqual(so.perf_cell(rec, hdr_on), "0.013")
        # a hostile "promote" table: positive dev, non-neutral verdict
        hostile = {"dev": 0.5, "verdict": "boost"}
        self.assertEqual(so.perf_req_of(hostile, hdr_on), 0.0)
        self.assertEqual(so.perf_req_at_full_weight(hostile), 0.0)
        self.assertNotIn("-", so.perf_cell(hostile, hdr_on))
        # and req can never exceed the file's MAX_REQ
        self.assertEqual(so.perf_req_of({"dev": -5.0}, hdr_on), 0.10)

    def test_perf_column_is_open_scan_only(self):
        rows = [_row("KXEOWEEK-26SEP19"), _row("KXOTHER-26OCT01")]
        plain = so.text_table(rows)
        self.assertNotIn("PERF", plain[0])
        with_perf = so.text_table(rows, perf={"KXEOWEEK-26SEP19": "(BAR)"})
        self.assertIn("PERF", with_perf[0])
        self.assertTrue(any(l.endswith("(BAR)") for l in with_perf))
        # the other tiers' tables are unchanged, header included
        self.assertEqual(plain[0], with_perf[0][:len(plain[0])])
        h = so.html_table(rows, perf={"KXEOWEEK-26SEP19": "(BAR)"})
        self.assertIn(">PERF<", h)
        self.assertIn(">(BAR)<", h)
        self.assertNotIn(">PERF<", so.html_table(rows))


class TestScanPerfCostLine(unittest.TestCase):
    """Judge must_fix: publish the COST side. Forgone rent is never observed
    as a loss, so the two-week review is biased toward keeping the loop unless
    the seats it left empty are printed next to the markout it bought."""

    def _sink(self, tmp, rows):
        by_day = {}
        for r in rows:
            by_day.setdefault(r["ts"][:10], []).append(r)
        for day, recs in by_day.items():
            with open(os.path.join(tmp, f"selection_events_{day}.jsonl"),
                      "a", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")

    def test_cost_line_arithmetic_from_a_fixture_day_file(self):
        tmp = tempfile.mkdtemp(prefix="perf_sink_")
        self.addCleanup(shutil.rmtree, tmp, True)
        now = datetime.now(timezone.utc)

        def rec(tk, dec, est, hours_ago):
            return {"ticker": tk, "decision": dec, "prev": None,
                    "series": tk.split("-")[0], "event_ticker": "E",
                    "est_dollars_per_day": est, "is_scan": True,
                    "ts": (now - timedelta(hours=hours_ago)).isoformat()}

        self._sink(tmp, [
            rec("KXA-26OCT01-T1", "perf_roi", 0.50, 1),
            # the same decision re-emitted after a restart (prev: null burst):
            # ~20 restarts a day, so a ROW count would scale with restarts
            rec("KXA-26OCT01-T1", "perf_roi", 0.50, 0.5),
            # below the exchange's MEASURED $1.00 minimum over a 7-day period
            rec("KXB-26OCT01-T1", "perf_roi", 0.10, 2),
            rec("KXC-26OCT01-T1", "perf_barred", 2.00, 3),
            # outside the 24h window
            rec("KXD-26OCT01-T1", "perf_roi", 5.00, 30),
            # not a perf decision at all
            rec("KXE-26OCT01-T1", "selected", 9.00, 1),
        ])
        c = sd.scan_perf_cost(now, status_dir=tmp)
        self.assertEqual(c["seats_empty"], 2)       # A and B, distinct tickers
        self.assertEqual(c["barred"], 1)            # C
        self.assertEqual(c["rows"], 4)              # rows include the re-emit
        self.assertEqual(c["floored_out"], 1)       # B never clears $1.00
        self.assertAlmostEqual(c["forgone_dollars_per_day"], 2.50, places=6)
        line = sd.scan_perf_cost_line(c)
        self.assertEqual(
            line,
            "seats left empty by perf_roi (24h): 2 of {} | admissions barred "
            "(24h): 1 | MODELLED forgone floored est on blocked candidates: "
            "$2.50 [MODELLED]".format(c["seats_cap"]))
        self.assertIn("[MODELLED]", line)

    def test_an_empty_sink_is_zeros_not_an_exception(self):
        tmp = tempfile.mkdtemp(prefix="perf_sink_")
        self.addCleanup(shutil.rmtree, tmp, True)
        c = sd.scan_perf_cost(datetime.now(timezone.utc), status_dir=tmp)
        self.assertEqual((c["seats_empty"], c["barred"], c["rows"]), (0, 0, 0))
        self.assertIn("0 of", sd.scan_perf_cost_line(c))

    def test_digest_one_liner(self):
        line = sd.scan_perf_digest_line(_table(), ZERO_COST, STATUS)
        self.assertTrue(line.startswith("perf: table "))
        self.assertIn("weight 0.0", line)
        self.assertIn("bar off", line)
        self.assertIn("1 down-ranked", line)
        self.assertIn("1 barred", line)
        self.assertIn("MODELLED rent forgone", line)
        self.assertIn("no scan-perf table on disk",
                      sd.scan_perf_digest_line({}, ZERO_COST, STATUS))
        self.assertIn("STALE",
                      sd.scan_perf_digest_line(_table(age_h=72), ZERO_COST,
                                               STATUS))

    def test_the_file_is_not_the_policy(self):
        """The email reads scan_perf.json; the BOT may have refused it whole
        (any schema failure is all-or-nothing) or not reloaded it yet, and in
        either case the tier is running the previous table or none. Rendering
        a refused file as a live policy, WEIGHT and BAR and all, is the "a
        model is not a measurement" failure at the reporting end: a hostile
        copy of the real table (every dev the string 'oops') printed as a
        normal header with '?' values and no hint the loader rejects it."""
        t = _table()
        status = {"scan_perf": {"weight": 0.0, "bar_enabled": False,
                                "fresh": True,
                                "generated_at": "2026-09-01T07:55:00Z",
                                "down_ranked": 0, "barred": 0}}
        hdr = sd.scan_perf_header(t, status)
        self.assertIs(hdr["loaded_by_bot"], False)
        banner = sd.scan_perf_not_loaded_banner(hdr)
        self.assertIn("HAS NOT LOADED THIS TABLE", banner)
        self.assertIn("2026-09-01T07:55:00Z", banner)
        line = sd.scan_perf_digest_line(t, ZERO_COST, status)
        self.assertIn("HAS NOT LOADED THIS TABLE", line)
        # the block says so too, above the verdicts
        body = "\n".join(so.scan_perf_text_block(t, hdr, ZERO_COST, {}))
        self.assertIn("HAS NOT LOADED THIS TABLE", body)
        # ... and when the bot IS running this table, no banner, and the
        # counts come from what it INSTALLED, not from the file's own limits
        # (the reader-side caps legitimately truncate them)
        status2 = {"scan_perf": {"weight": 0.0, "bar_enabled": False,
                                 "fresh": True,
                                 "generated_at": t["generated_at"],
                                 "down_ranked": 25, "barred": 5}}
        hdr2 = sd.scan_perf_header(t, status2)
        self.assertIs(hdr2["loaded_by_bot"], True)
        self.assertEqual(sd.scan_perf_not_loaded_banner(hdr2), "")
        line2 = sd.scan_perf_digest_line(t, ZERO_COST, status2)
        self.assertIn("25 down-ranked in force", line2)
        self.assertIn("5 barred", line2)
        # a status file that cannot say (no scan_perf block) must not cry wolf
        hdr3 = sd.scan_perf_header(t, {})
        self.assertIsNone(hdr3["loaded_by_bot"])
        self.assertEqual(sd.scan_perf_not_loaded_banner(hdr3), "")


class TestPerfRecordKeying(unittest.TestCase):
    """The scorer keys `events` by EVENT ROOT and `series` by SERIES. Those
    are the only two keyings the schema has, and the root rule is the bot's
    own (incentive_mm.scan_perf_event_root), verbatim — 30 *CC events left
    this email silently on 2026-09-10, so a miss must be a real absence and
    not a keying mismatch."""

    def test_root_rule_matches_the_bot_and_the_scorer(self):
        import incentive_mm as imm_mod
        import imm_scan_perf as sp
        for ev in ("KXCPIYOY-26NOV", "KXAXP-26OCTCARDS",
                   "KXHYPEMINMON-HYPE-26JUL31", "KXEOWEEK-26SEP19",
                   "KXVOTEGENERAL-HOUSECO3-26CBRO", "KXMLBPLAYOFFS"):
            self.assertEqual(so.perf_event_root(ev),
                             imm_mod.scan_perf_event_root(ev), ev)
            self.assertEqual(so.perf_event_root(ev), sp.root_of(ev), ev)

    def test_lookup_finds_the_root_record_and_falls_back_to_series(self):
        t = _table()
        k, unit, rec = so.perf_record(t, "KXEOWEEK-26SEP19")
        self.assertEqual((k, unit), ("KXEOWEEK", "event"))
        self.assertEqual(rec["verdict"], "bar")
        # a later re-listing of the same weekly hits the SAME root record
        k2, unit2, _ = so.perf_record(t, "KXEOWEEK-26OCT03")
        self.assertEqual((k2, unit2), ("KXEOWEEK", "event"))
        # no event record -> the series record, by series key
        k3, unit3, rec3 = so.perf_record(t, "KXCPIYOY-26NOV")
        self.assertEqual((k3, unit3), ("KXCPIYOY", "series"))
        self.assertEqual(rec3["verdict"], "down_rank")
        # and an unscored event is a real absence
        self.assertEqual(so.perf_record(t, "KXNOTHING-26DEC01"),
                         (None, None, None))

    def test_every_root_in_a_real_scorer_table_is_reachable(self):
        """Pinned against the scorer's own shape: every key in the `events`
        block must be exactly root_of() of the dated events it covers, so the
        email can always find it."""
        src = os.environ.get("IMM_SCAN_PERF_TEST_TABLE", "")
        if not src or not os.path.exists(src):
            self.skipTest("IMM_SCAN_PERF_TEST_TABLE unset — see "
                          "test_imm_scan_perf for the one-liner")
        with open(src, encoding="utf-8") as f:
            table = json.load(f)
        for root, rec in (table.get("events") or {}).items():
            for ev in rec.get("events") or []:
                self.assertEqual(so.perf_event_root(ev), root, ev)
                self.assertIsNot(so.perf_record(table, ev)[2], None)


if __name__ == "__main__":
    unittest.main()
