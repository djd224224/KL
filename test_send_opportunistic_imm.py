#!/usr/bin/env python3
"""Unit tests for send_opportunistic_imm.py's new-since-last-email marking
(Jack 2026-09-13: "always bold the events that are new (werent in the
previous email)") and for the SCAN PERF block + THE COST LINE.
Run: python -m unittest test_send_opportunistic_imm"""

import json
import re
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


def _rec(kind, verdict, roi, n, *, until=None, events=None, members=None,
         match=None, cred=0.0, realized=0.0, mtm=0.0, rent=0.0, basis="est_floored"):
    r = {"kind": kind, "key": "?", "n_markets": 3, "n_events": 1, "risk_days": n,
         "roi_hist": roi, "roi_hist_trading": roi, "roi_hist_measured": roi,
         "rent_used": rent, "rent_modelled": rent, "rent_measured_dollars": 0.0,
         "rent_basis": basis, "credited_measured": cred, "realized_dollars": realized,
         "mtm_dollars": mtm, "net_dollars": rent + realized + mtm,
         "verdict": verdict, "reason": "fixture"}
    if verdict == "block":
        r["until"] = until or (datetime.now(timezone.utc) + timedelta(hours=60)
                               ).strftime("%Y-%m-%dT%H:%M:%SZ")
    if events is not None:
        r["events"] = events
        r["n_events"] = len(events)
    if members is not None:
        r["members"] = members
    if match is not None:
        r["match"] = match
    return r


def _table(age_h=4.0, **over):
    """A scan_perf.json in the v2 schema, small enough to read in the
    assertions below. The tier numbers are the 2026-09-20 MEASURED ones so a
    mislabelling shows up as a wrong figure, not as a plausible one."""
    gen = (datetime.now(timezone.utc)
           - timedelta(hours=age_h)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t = {
        "version": 2,
        "generated_at": gen,
        "params": {"min_risk_days": 100.0, "block_roi": 0.0, "block_ttl_hours": 60},
        "coverage": {"markets": 197, "events": 110, "series": 100, "fills": 171,
                     "risk_days": 21379.9, "credited_events": 2},
        "tier": {"roi_hist": 0.00376, "roi_hist_trading": -0.01667,
                 "roi_hist_measured": -0.00612, "rent_used_dollars": 436.81,
                 "rent_modelled_dollars": 436.81, "rent_basis": "est_floored",
                 "rent_measured_dollars": 0.0, "realized_dollars": -130.79,
                 "mtm_dollars": -225.69, "credited_measured_dollars": 21.28,
                 "ledger_stale_days": 9},
        # events are keyed by EVENT ROOT. KXTOKENUSE's root record ALLOWS
        # while its series record BLOCKS -- on purpose, so the hierarchy is
        # testable (a re-listed event is judged on its own root first).
        "events": {"KXTOKENUSE": _rec("event", "allow", 0.0652, 416.1,
                                      events=["KXTOKENUSE-26SEP21"]),
                   "KXEOWEEK": _rec("event", "allow", 0.0060, 168.9,
                                    events=["KXEOWEEK-26SEP12", "KXEOWEEK-26SEP19"],
                                    cred=9.45),
                   "KXGAS-NORTH": _rec("event", "block", -0.05, 300.0,
                                       events=["KXGAS-NORTH-26OCT01"])},
        "series": {"KXCPIYOY": _rec("series", "block", -0.0333, 762.3,
                                    until="2026-10-02T11:55:04Z",
                                    rent=13.15, realized=-10.45, mtm=-28.10),
                   "KXTRUMPENDORSEMENTS": _rec("series", "allow", 0.1452, 582.9,
                                               rent=106.06, realized=-21.45),
                   "KXTOKENUSE": _rec("series", "block", -0.20, 416.1),
                   "KXTHIN": _rec("series", "insufficient", -0.5, 40.0)},
        "families": {"FISCAL_KPI": _rec("family", "block", -0.0242, 2733.8,
                                        members=["KXAXP", "KXAAL"],
                                        match={"fiscal": True}),
                     "CPI": _rec("family", "block", -0.0187, 2656.9,
                                 members=["KXCPI", "KXCPIYOY"],
                                 match={"regex": r"^KXCPI(CORE)?(YOY)?$"}),
                     "AAAGAS_STATE_DAILY": _rec("family", "insufficient", 0.0, 0.0,
                                                members=[],
                                                match={"regex": r"^KXAAAGASD[A-Z]{2}$"})},
        "limits": {"blocked_events": 1, "blocked_series": 2, "blocked_families": 2,
                   "n_units": 10},
        "unmatched_block_keys": ["series:KXGONE"],
        "warnings": ["credit ledger 9 days stale (newest 2026-09-11); rent is MODELLED"],
    }
    t.update(over)
    return t


ZERO_COST = {"window_h": 24, "seats_at_risk": 0, "blocked": 0, "floored_out": 0,
             "forgone_dollars_per_day": 0.0, "seats_cap": 60}
STATUS = {"scan_perf": {"block_enabled": True, "blend_enabled": True, "blend_m": 200.0}}
STATUS_OFF = {"scan_perf": {"block_enabled": False, "blend_enabled": False,
                            "blend_m": 200.0}}


class TestScanPerfBlock(unittest.TestCase):
    """Jack 2026-09-06: "a model is not a measurement". The block puts the
    judging units' MEASURED history in front of a human with the rent basis
    on every row, the MEASURED credits in their own column, and BLOCK/BLEND
    stated explicitly so a table of intentions cannot be read as a table of
    actions."""

    def test_block_renders_from_a_fixture_table(self):
        t = _table()
        hdr = sd.scan_perf_header(t, STATUS)
        body = "\n".join(so.scan_perf_text_block(
            t, hdr, ZERO_COST, so.perf_verdict_map(t)))
        self.assertIn("BLOCK on", body)
        self.assertIn("BLEND on (m=200)", body)
        self.assertIn("bot status file", body)      # says WHICH source
        self.assertNotIn("STALE", body)
        self.assertIn("rule: a unit with >= 100 $-days", body)
        # units: blocks first, with their TTL; allows after
        rows = [l for l in body.splitlines()
                if l.startswith(("KXCPIYOY", "KXTRUMPENDORSEMENTS", "FISCAL_KPI",
                                 "KXGAS-NORTH", "KXEOWEEK"))]
        self.assertTrue(rows)
        self.assertLess(body.index("KXCPIYOY"), body.index("KXTRUMPENDORSEMENTS"))
        cpi = next(l for l in rows if l.startswith("KXCPIYOY"))
        self.assertIn("block", cpi)
        self.assertIn("2026-10-02", cpi)
        self.assertIn("-0.0333", cpi)
        self.assertIn("est", cpi)                   # the rent basis, per row
        trump = next(l for l in rows if l.startswith("KXTRUMPENDORSEMENTS"))
        self.assertIn("+0.1452", trump)
        self.assertIn("allow", trump)
        # an event root that IS a series key is listed once, as the series
        self.assertEqual(sum(1 for l in body.splitlines()
                             if l.startswith("KXTOKENUSE")), 1)
        # the insufficient unit is not a row but is counted
        self.assertNotIn("KXTHIN", body.split("FAMILIES")[0].split("UNITS")[1])
        self.assertIn("2 unit(s) under the floor", body)
        # families, blocks line, unmatched keys, warnings
        self.assertIn("FISCAL_KPI", body)
        self.assertIn("KXAXP, KXAAL", body)
        self.assertIn("blocks: 1 event root(s), 2 series, 2 family(ies)", body)
        self.assertIn("series:KXGONE", body)
        self.assertIn("credit ledger 9 days stale", body)

    def test_measured_credits_are_a_separate_column_never_added_to_est(self):
        """The accrued_est/credited trap, one level down: rent here is
        MODELLED est and credited is MEASURED money, and 436.81 + 21.28 must
        appear NOWHERE."""
        t = _table()
        hdr = sd.scan_perf_header(t, STATUS)
        body = "\n".join(so.scan_perf_text_block(
            t, hdr, ZERO_COST, so.perf_verdict_map(t)))
        self.assertIn("436.81", body)
        self.assertIn("21.28", body)
        self.assertNotIn("458.09", body)
        self.assertIn("never", body.split("MEASURED credits")[1][:200])
        row = [l for l in body.splitlines() if l.startswith("KXEOWEEK")][0]
        self.assertTrue(row.rstrip().endswith("9.45"), row)

    def test_stale_table_shows_the_banner_and_applies_nothing(self):
        t = _table(age_h=72.0)
        hdr = sd.scan_perf_header(t, STATUS)
        self.assertTrue(hdr["stale"])
        body = "\n".join(so.scan_perf_text_block(
            t, hdr, ZERO_COST, so.perf_verdict_map(t)))
        self.assertIn("STALE — verdicts not applied", body)
        _k, _u, rec = so.perf_record(t, "KXCPIYOY-26NOV")
        self.assertEqual(so.perf_cell(rec, hdr), "(BLOCK)")
        _k, _u, rec = so.perf_record(t, "KXTRUMPENDORSEMENTS-26SEP25")
        self.assertEqual(so.perf_cell(rec, hdr), "(+0.145)")
        html = so.scan_perf_html_block(t, hdr, ZERO_COST, so.perf_verdict_map(t))
        self.assertEqual(html.count("STALE"), 1, "stale banner rendered twice")

    def test_missing_table_renders_no_table_and_raises_nothing(self):
        """The email must never fail because of this block."""
        self.assertEqual(sd.load_scan_perf(os.path.join(
            tempfile.gettempdir(), "definitely_not_a_table_12345.json")), {})
        hdr = sd.scan_perf_header({}, None)
        self.assertFalse(hdr["present"])
        body = "\n".join(so.scan_perf_text_block({}, hdr, ZERO_COST, None))
        self.assertIn("no table", body)
        self.assertIn("admissions blocked", body)
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
            json.dump({"version": 2}, f)
        self.assertEqual(sd.load_scan_perf(nogen), {})

    def test_a_changed_verdict_is_marked_and_bolded(self):
        t = _table()
        hdr = sd.scan_perf_header(t, STATUS)
        prev = {"series:KXCPIYOY": "allow", "family:CPI": "block",
                "series:KXTRUMPENDORSEMENTS": "allow"}
        body = "\n".join(so.scan_perf_text_block(t, hdr, ZERO_COST, prev))
        self.assertIn("!KXCPIYOY", body)          # allow -> block
        self.assertNotIn("!CPI", body)            # unchanged
        self.assertNotIn("!KXTRUMPENDORSEMENTS", body)
        html = so.scan_perf_html_block(t, hdr, ZERO_COST, prev)
        self.assertIn("<b>!KXCPIYOY", html)
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
        self.assertEqual(v, {"event:KXEOWEEK": "allow",
                             "event:KXGAS-NORTH": "block",
                             "series:KXCPIYOY": "block",
                             "series:KXTRUMPENDORSEMENTS": "allow",
                             "series:KXTOKENUSE": "block",
                             "family:FISCAL_KPI": "block",
                             "family:CPI": "block"})
        so.write_last_sent(datetime.now(timezone.utc), ["KXA-26OCT01"], v)
        self.assertEqual(so.previous_perf_verdicts(), v)

    def test_perf_cell_follows_the_hierarchy_and_the_knobs(self):
        t = _table()
        hdr_on = sd.scan_perf_header(t, STATUS)
        hdr_off = sd.scan_perf_header(t, STATUS_OFF)
        # a re-listed weekly is judged on its ROOT, which allows here even
        # though the series record blocks
        k, unit, rec = so.perf_record(t, "KXTOKENUSE-26SEP28")
        self.assertEqual((k, unit), ("KXTOKENUSE", "event"))
        self.assertEqual(so.perf_cell(rec, hdr_on), "+0.065")
        self.assertEqual(so.perf_cell(rec, hdr_off), "(+0.065)")
        k, unit, rec = so.perf_record(t, "KXCPIYOY-26NOV")
        self.assertEqual((k, unit), ("KXCPIYOY", "series"))
        self.assertEqual(so.perf_cell(rec, hdr_on), "BLOCK")
        self.assertEqual(so.perf_cell(rec, hdr_off), "(BLOCK)")
        # a new series inside a grouped family: members, then the regex
        self.assertEqual(so.perf_record(t, "KXAXP-26OCTCARDS")[:2],
                         ("FISCAL_KPI", "family"))
        self.assertEqual(so.perf_record(t, "KXCPICORE-26NOV")[:2], ("CPI", "family"))
        # under the floor -> no verdict; unknown -> no verdict
        self.assertEqual(so.perf_record(t, "KXTHIN-26OCT01"), (None, None, None))
        self.assertEqual(so.perf_record(t, "KXNOTHING-26DEC01"), (None, None, None))
        # a hostile record renders nothing
        self.assertEqual(so.perf_cell({"verdict": "boost", "roi_hist": 9.0}, hdr_on), "")

    def test_perf_column_is_open_scan_only(self):
        rows = [_row("KXEOWEEK-26SEP19"), _row("KXOTHER-26OCT01")]
        plain = so.text_table(rows)
        self.assertNotIn("PERF", plain[0])
        with_perf = so.text_table(rows, perf={"KXEOWEEK-26SEP19": "(BLOCK)"})
        self.assertIn("PERF", with_perf[0])
        self.assertTrue(any(l.endswith("(BLOCK)") for l in with_perf))
        self.assertEqual(plain[0], with_perf[0][:len(plain[0])])
        h = so.html_table(rows, perf={"KXEOWEEK-26SEP19": "(BLOCK)"})
        self.assertIn(">PERF<", h)
        self.assertIn(">(BLOCK)<", h)
        self.assertNotIn(">PERF<", so.html_table(rows))


class TestScanPerfCostLine(unittest.TestCase):
    """Publish the COST side. Forgone rent is never observed as a loss, so a
    review is biased toward keeping the loop unless the admissions it
    refused are printed next to the history it acted on."""

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
            rec("KXA-26OCT01-T1", "perf_roi", 0.50, 0.5),      # restart re-emit
            rec("KXB-26OCT01-T1", "perf_roi", 0.10, 2),        # under the $1 floor
            rec("KXC-26OCT01-T1", "perf_barred", 2.00, 3),
            rec("KXD-26OCT01-T1", "perf_roi", 5.00, 30),       # outside 24h
            rec("KXE-26OCT01-T1", "selected", 9.00, 1),        # not a perf decision
        ])
        c = sd.scan_perf_cost(now, status_dir=tmp)
        self.assertEqual(c["seats_at_risk"], 2)
        self.assertEqual(c["blocked"], 1)
        self.assertEqual(c["rows"], 4)
        self.assertEqual(c["floored_out"], 1)
        self.assertAlmostEqual(c["forgone_dollars_per_day"], 2.50, places=6)
        line = sd.scan_perf_cost_line(c)
        self.assertEqual(
            line,
            "admissions blocked (24h): 1 | seats at risk from the blend (24h, "
            "UPPER BOUND): 2 of {} | MODELLED forgone floored est on refused "
            "candidates: $2.50 [MODELLED]".format(c["seats_cap"]))

    def test_an_empty_sink_is_zeros_not_an_exception(self):
        tmp = tempfile.mkdtemp(prefix="perf_sink_")
        self.addCleanup(shutil.rmtree, tmp, True)
        c = sd.scan_perf_cost(datetime.now(timezone.utc), status_dir=tmp)
        self.assertEqual((c["seats_at_risk"], c["blocked"], c["rows"]), (0, 0, 0))
        self.assertIn("0 of", sd.scan_perf_cost_line(c))

    def test_digest_one_liner(self):
        line = sd.scan_perf_digest_line(_table(), ZERO_COST, STATUS)
        self.assertTrue(line.startswith("perf: table "))
        self.assertIn("block on", line)
        self.assertIn("blend on (m=200)", line)
        self.assertIn("blocked 1 event root(s) / 2 series / 2 family(ies) in file", line)
        self.assertIn("MODELLED rent forgone", line)
        self.assertIn("no scan-perf table on disk",
                      sd.scan_perf_digest_line({}, ZERO_COST, STATUS))
        self.assertIn("STALE",
                      sd.scan_perf_digest_line(_table(age_h=72), ZERO_COST, STATUS))

    def test_the_file_is_not_the_policy(self):
        """The email reads scan_perf.json; the BOT may have refused it whole
        or not reloaded it yet. Rendering a refused file as a live policy is
        the 'a model is not a measurement' failure at the reporting end."""
        t = _table()
        status = {"scan_perf": {"block_enabled": True, "blend_enabled": True,
                                "blend_m": 200.0, "fresh": True,
                                "generated_at": "2026-09-01T07:55:00Z",
                                "blocked_events": 0, "blocked_series": 0,
                                "blocked_families": 0}}
        hdr = sd.scan_perf_header(t, status)
        self.assertIs(hdr["loaded_by_bot"], False)
        banner = sd.scan_perf_not_loaded_banner(hdr)
        self.assertIn("HAS NOT LOADED THIS TABLE", banner)
        self.assertIn("2026-09-01T07:55:00Z", banner)
        self.assertIn("HAS NOT LOADED THIS TABLE",
                      sd.scan_perf_digest_line(t, ZERO_COST, status))
        body = "\n".join(so.scan_perf_text_block(t, hdr, ZERO_COST, {}))
        self.assertIn("HAS NOT LOADED THIS TABLE", body)
        # when the bot IS running this table: no banner, counts come from
        # what it INSTALLED (the reader-side block cap can drop records)
        status2 = {"scan_perf": {"block_enabled": True, "blend_enabled": True,
                                 "blend_m": 200.0, "fresh": True,
                                 "generated_at": t["generated_at"],
                                 "blocked_events": 1, "blocked_series": 1,
                                 "blocked_families": 2, "judged_24h": 41}}
        hdr2 = sd.scan_perf_header(t, status2)
        self.assertIs(hdr2["loaded_by_bot"], True)
        self.assertEqual(sd.scan_perf_not_loaded_banner(hdr2), "")
        line2 = sd.scan_perf_digest_line(t, ZERO_COST, status2)
        self.assertIn("blocked 1 event root(s) / 1 series / 2 family(ies) in force", line2)
        self.assertIn("41 candidate(s) judged (24h)", line2)
        hdr3 = sd.scan_perf_header(t, {})
        self.assertIsNone(hdr3["loaded_by_bot"])
        self.assertEqual(sd.scan_perf_not_loaded_banner(hdr3), "")
        self.assertEqual(hdr3["source"], "incentive_mm code default")


class TestPerfRecordKeying(unittest.TestCase):
    """The scorer keys `events` by EVENT ROOT, `series` by series and
    `families` by the group name; the root rule is the bot's own
    (incentive_mm.scan_perf_event_root), verbatim."""

    def test_root_rule_matches_the_bot_and_the_scorer(self):
        import incentive_mm as imm_mod
        import imm_scan_perf as sp
        for ev in ("KXCPIYOY-26NOV", "KXAXP-26OCTCARDS", "KXTOKENUSE-26SEP28",
                   "KXHYPEMINMON-HYPE-26JUL31", "KXEOWEEK-26SEP19",
                   "KXVOTEGENERAL-HOUSECO3-26CBRO", "KXMLBPLAYOFFS"):
            self.assertEqual(so.perf_event_root(ev),
                             imm_mod.scan_perf_event_root(ev), ev)
            self.assertEqual(so.perf_event_root(ev), sp.root_of(ev), ev)

    def test_family_match_agrees_with_the_bot(self):
        """The email's family resolution (members, then regex) must agree
        with the bot's for everything the file can prove."""
        import incentive_mm as imm_mod
        t = _table()
        saved = (dict(imm_mod.SCAN_PERF_FAMILIES), dict(imm_mod._scan_perf_state))
        try:
            imm_mod.SCAN_PERF_FAMILIES.clear()
            for name, rec in t["families"].items():
                imm_mod.SCAN_PERF_FAMILIES[name] = {
                    "members": frozenset(rec["members"]),
                    "regex_c": (re.compile(rec["match"]["regex"])
                                if rec["match"].get("regex") else None),
                    "fiscal": bool(rec["match"].get("fiscal"))}
            for s in ("KXAXP", "KXCPICORE", "KXAAAGASDTX", "KXOTHER", "KXCPIYOY"):
                self.assertEqual(so.perf_family_of(t, s),
                                 imm_mod.scan_perf_family_of(s), s)
        finally:
            imm_mod.SCAN_PERF_FAMILIES.clear()
            imm_mod.SCAN_PERF_FAMILIES.update(saved[0])
            imm_mod._scan_perf_state.clear()
            imm_mod._scan_perf_state.update(saved[1])

    def test_every_root_in_a_real_scorer_table_is_reachable(self):
        """Pinned against the scorer's own shape: every key in the `events`
        block must be exactly root_of() of the dated events it covers, so
        the email can always find it."""
        src = os.environ.get("IMM_SCAN_PERF_TEST_TABLE", "")
        if not src or not os.path.exists(src):
            self.skipTest("IMM_SCAN_PERF_TEST_TABLE unset — see "
                          "test_imm_scan_perf for the one-liner")
        with open(src, encoding="utf-8") as f:
            table = json.load(f)
        for root, rec in (table.get("events") or {}).items():
            for ev in rec.get("events") or []:
                self.assertEqual(so.perf_event_root(ev), root, ev)
                k, unit, got = so.perf_record(table, ev, rec.get("series"))
                if rec.get("verdict") != "insufficient":
                    self.assertEqual((k, unit), (root, "event"), ev)


if __name__ == "__main__":
    unittest.main()
