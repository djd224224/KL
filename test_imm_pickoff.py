#!/usr/bin/env python3
"""Unit tests for imm_pickoff.py -- PICK-OFF WINDOWS, the events whose Kalshi
event start (its milestone, what "at event start" orders expire on) is LATER
than the real event (Jack 2026-09-27).

Run: python -m unittest test_imm_pickoff

The two ways this can lie are the whole test surface:
  * shout about a window that is not there. The normal wait between an
    earnings release and its call (0.5-4h, or next morning for a split
    reporter like TOL) looks exactly like "Kalshi is late" if the bar is
    wrong, and Kalshi being EARLY (CCL/NKE, Sep 2026) is never a window;
  * miss a real one. LLY (+49.5h), DELL (+48.5h, a one_off_milestone, not a
    company_report) and BULL (Nasdaq-only: no override, no program) are the
    shapes that actually happened.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import incentive_mm as imm                                          # noqa: E402
import imm_pickoff as ip                                            # noqa: E402

ET = imm.ET
# Mon 2026-10-05 07:10 ET -- clear of every hard-coded July override.
NOW = datetime(2026, 10, 5, 11, 10, tzinfo=timezone.utc)
EARN = "KXEARNINGSMENTION"


def setUpModule():
    """Sandbox the overrides file away from the live run-logs directory."""
    tmp = tempfile.mkdtemp(prefix="imm_pickoff_test_")
    imm.STATUS_DIR = tmp
    imm.ALERT_RECIPIENTS = []
    imm.EVENT_OVERRIDES_FILE = os.path.join(tmp, "event_start_overrides.json")


def et(y, mo, d, h, mi=0) -> datetime:
    return ET.localize(datetime(y, mo, d, h, mi))


def zulu(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def milestone(ev, start, title="", typ="company_report", primary=True):
    return {"type": typ, "title": title or ev, "start_date": zulu(start),
            "end_date": zulu(start + timedelta(hours=2)),
            "primary_event_tickers": [ev] if primary else [],
            "related_event_tickers": [ev]}


class FakeClient:
    """Pages /milestones (honouring minimum_start_date and cursors) and
    answers /events/<ticker> with the given market statuses."""

    def __init__(self, milestones, statuses=None, page_size=500, boom=False):
        self.milestones = list(milestones)
        self.statuses = statuses or {}
        self.page_size = page_size
        self.boom = boom
        self.calls = []

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if self.boom:
            raise RuntimeError("network down")
        if path == "/milestones":
            rows = self.milestones
            if "related_event_ticker" in params:
                ev = params["related_event_ticker"]
                return {"milestones": [m for m in rows
                                       if ev in m["related_event_tickers"]],
                        "cursor": ""}
            since = params.get("minimum_start_date")
            if since:
                rows = [m for m in rows if m["start_date"] >= since]
            start = int(params.get("cursor") or 0)
            nxt = start + self.page_size
            return {"milestones": rows[start:nxt],
                    "cursor": str(nxt) if nxt < len(rows) else ""}
        if path.startswith("/events/"):
            ev = path[len("/events/"):]
            if self.statuses.get(ev) == "raise":
                raise RuntimeError("read failed")
            return {"event": {"markets": [
                {"status": s} for s in self.statuses.get(ev, ["active"])]}}
        return {}


class PickoffCase(unittest.TestCase):

    def setUp(self):
        self._write_overrides({})

    def tearDown(self):
        self._write_overrides({})

    def _write_overrides(self, data):
        with open(imm.EVENT_OVERRIDES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
        imm._file_override_state["mtime"] = 0.0
        imm.load_file_event_overrides()

    def scan(self, overrides, milestones, meta=None, nasdaq=None,
             statuses=None, now=NOW):
        self._write_overrides({ev: dt.isoformat()
                               for ev, dt in overrides.items()})
        client = FakeClient(milestones, statuses)
        res = ip.scan(client, now, meta=meta or {},
                      nasdaq_for_date=lambda d: (nasdaq or {}).get(d, {}))
        self.assertIsNone(res["error"])
        return res

    def flagged(self, res):
        return [r["event"] for r in res["rows"]]


class TestWhatCountsAsAWindow(PickoffCase):

    def test_lly_shape_kalshi_two_days_late_on_a_before_open_report(self):
        ev = EARN + "LLY-26OCT07"
        res = self.scan({ev: et(2026, 10, 5, 7)},
                        [milestone(ev, et(2026, 10, 7, 8, 30),
                                   "Eli Lilly Earnings Call")])
        self.assertEqual(self.flagged(res), [ev])
        r = res["rows"][0]
        self.assertTrue(r["open_now"])            # 07:10 is past the 07:00 release
        self.assertEqual(r["kind"], "before_open")
        self.assertAlmostEqual(r["gap_h"], 49.5)

    def test_dell_shape_after_close_and_a_one_off_milestone(self):
        # DELL's call milestone was typed one_off_milestone: a sweep filtered
        # to company_report would never have seen it.
        ev = EARN + "DELL-26OCT08"
        res = self.scan({ev: et(2026, 10, 6, 16)},
                        [milestone(ev, et(2026, 10, 8, 16, 30),
                                   "Dell Earnings Call",
                                   typ="one_off_milestone")])
        self.assertEqual(self.flagged(res), [ev])
        self.assertFalse(res["rows"][0]["open_now"])

    def test_split_reporter_next_morning_call_is_not_a_window(self):
        # TOL: release after the close, call 08:30 the next morning, and
        # Kalshi had it right. +16.5h must stay under the after-close bar.
        ev = EARN + "TOL-26OCT06"
        res = self.scan({ev: et(2026, 10, 6, 16)},
                        [milestone(ev, et(2026, 10, 7, 8, 30))])
        self.assertEqual(self.flagged(res), [])
        self.assertEqual(res["checked"], 1)

    def test_after_close_bar_is_noon_the_next_day(self):
        ev = EARN + "ABC-26OCT06"
        on_bar = self.scan({ev: et(2026, 10, 6, 16)},
                           [milestone(ev, et(2026, 10, 7, 12))])
        past_bar = self.scan({ev: et(2026, 10, 6, 16)},
                             [milestone(ev, et(2026, 10, 7, 12, 30))])
        self.assertEqual(self.flagged(on_bar), [])
        self.assertEqual(self.flagged(past_bar), [ev])

    def test_normal_release_to_call_gap_is_not_a_window(self):
        # AZO/DE/WING: 07:00 release proxy, 10:00 call -- the milestone is
        # the CALL, so a 3h gap is Kalshi being right.
        ev = EARN + "AZO-26OCT06"
        res = self.scan({ev: et(2026, 10, 6, 7)},
                        [milestone(ev, et(2026, 10, 6, 10))])
        self.assertEqual(self.flagged(res), [])

    def test_kalshi_early_is_never_a_window(self):
        # CCL/NKE, Sep 2026: the orders are pulled BEFORE the real call.
        ccl, nke = EARN + "CCL-26OCT05", EARN + "NKE-26OCT06"
        res = self.scan({ccl: et(2026, 10, 6, 7), nke: et(2026, 10, 8, 16)},
                        [milestone(ccl, et(2026, 10, 5, 10)),
                         milestone(nke, et(2026, 10, 6, 17))])
        self.assertEqual(self.flagged(res), [])

    def test_broadcast_override_hours_ahead_is_not_a_window(self):
        # The Trump overrides stand down at the programme start, 2-5h before
        # the milestone's speech time. A whole day is a different matter.
        ev = "KXTRUMPMENTION-26OCT06"
        meta = {ev: {"iso": et(2026, 10, 6, 19).isoformat(),
                     "confidence": "read", "label": "broadcast schedule"}}
        hours = self.scan({ev: et(2026, 10, 6, 19)},
                          [milestone(ev, et(2026, 10, 6, 21),
                                     typ="one_off_milestone")], meta=meta)
        day = self.scan({ev: et(2026, 10, 6, 19)},
                        [milestone(ev, et(2026, 10, 7, 19),
                                   typ="one_off_milestone")], meta=meta)
        self.assertEqual(self.flagged(hours), [])
        self.assertEqual(self.flagged(day), [ev])
        self.assertEqual(day["rows"][0]["label"], "broadcast schedule")

    def test_a_guessed_hour_only_counts_a_whole_day_late(self):
        # A fail-safe 07:00 is not a measured before-open: the real call may
        # be that evening, so the bar is noon the next day, not 07:00 + 6h.
        ev = EARN + "CELH-26OCT06"
        iso = et(2026, 10, 6, 7)
        meta = {ev: {"iso": iso.isoformat(), "confidence": "guess",
                     "label": "time n/a->7am ET BMO assumed (Nasdaq, fail-safe)"}}
        same_day = self.scan({ev: iso}, [milestone(ev, et(2026, 10, 6, 17))],
                             meta=meta)
        next_day = self.scan({ev: iso}, [milestone(ev, et(2026, 10, 7, 17))],
                             meta=meta)
        self.assertEqual(self.flagged(same_day), [])
        self.assertEqual(self.flagged(next_day), [ev])
        self.assertEqual(next_day["rows"][0]["kind"], "no_hour")

    def test_stale_provenance_is_ignored(self):
        # the sidecar describes an OLD value; the live 07:00 is a measured
        # hour now, so the before-open 6h bar applies
        ev = EARN + "CELH-26OCT06"
        meta = {ev: {"iso": et(2026, 10, 6, 16).isoformat(),
                     "confidence": "guess", "label": "time n/a->4pm ET"}}
        res = self.scan({ev: et(2026, 10, 6, 7)},
                        [milestone(ev, et(2026, 10, 6, 17))], meta=meta)
        self.assertEqual(self.flagged(res), [ev])
        self.assertEqual(res["rows"][0]["kind"], "before_open")


class TestNasdaqFallback(PickoffCase):
    """Most earnings-mention events carry no reward program and so no
    override (JPM, GS, BAC, PEP... on 2026-09-27). BULL-26JUL24 was a real
    window only Nasdaq could see."""

    def test_bull_shape_no_override_no_hour(self):
        ev = EARN + "BULL-26JUL24"
        res = self.scan({}, [milestone(ev, et(2026, 10, 14, 17),
                                       "Webull Earnings Call")],
                        nasdaq={"2026-10-06": {"BULL": "time-not-supplied"}})
        self.assertEqual(self.flagged(res), [ev])
        r = res["rows"][0]
        self.assertEqual(r["source"], "Nasdaq calendar")
        self.assertEqual(r["kind"], "no_hour")
        self.assertIn("Tue Oct 06 (no time published", ip.real_str(r))

    def test_a_before_open_flag_with_a_same_morning_call_is_quiet(self):
        ev = EARN + "PEP-26OCT08"
        res = self.scan({}, [milestone(ev, et(2026, 10, 8, 8, 15))],
                        nasdaq={"2026-10-08": {"PEP": "time-pre-market"}})
        self.assertEqual(self.flagged(res), [])
        self.assertEqual(res["checked"], 1)

    def test_an_override_beats_nasdaq(self):
        ev = EARN + "XYZ-26OCT09"
        res = self.scan({ev: et(2026, 10, 9, 7)},
                        [milestone(ev, et(2026, 10, 9, 10))],
                        nasdaq={"2026-10-06": {"XYZ": "time-pre-market"}})
        self.assertEqual(self.flagged(res), [])

    def test_a_series_nasdaq_does_not_list_is_skipped(self):
        ev = EARN + "ARITZIA-26OCT14"
        res = self.scan({}, [milestone(ev, et(2026, 10, 14, 10))],
                        nasdaq={"2026-10-06": {"ATZ": "time-pre-market"}})
        self.assertEqual((self.flagged(res), res["checked"]), ([], 0))

    def test_a_non_earnings_event_without_an_override_is_skipped(self):
        res = self.scan({}, [milestone("KXFOO-26OCT09",
                                       et(2026, 10, 9, 10))])
        self.assertEqual(res["checked"], 0)


class TestWindowLifetime(PickoffCase):

    EV = EARN + "LLY-26OCT07"

    def _lly(self, **kw):
        return self.scan({self.EV: et(2026, 10, 5, 7)},
                         [milestone(self.EV, et(2026, 10, 7, 8, 30))], **kw)

    def test_closed_markets_end_the_window(self):
        # Kalshi closes the markets once the real call is over (LLY 12:08).
        res = self._lly(statuses={self.EV: ["finalized", "closed"]})
        self.assertEqual(self.flagged(res), [])

    def test_an_unreadable_status_keeps_the_row_and_says_so(self):
        res = self._lly(statuses={self.EV: "raise"})
        self.assertEqual(self.flagged(res), [self.EV])
        self.assertIn("market status unread",
                      "\n".join(ip.text_lines(res, NOW)))

    def test_a_passed_kalshi_start_is_not_a_window(self):
        res = self._lly(now=et(2026, 10, 7, 9).astimezone(timezone.utc))
        self.assertEqual(self.flagged(res), [])

    def test_a_real_event_beyond_the_lookahead_is_not_reported(self):
        ev = EARN + "FAR-26NOV20"
        far = NOW + timedelta(days=ip.LOOKAHEAD_DAYS + 2)
        res = self.scan({ev: far}, [milestone(ev, far + timedelta(days=3))])
        self.assertEqual((self.flagged(res), res["checked"]), ([], 0))

    def test_next_quarters_event_is_not_this_quarters_window(self):
        # reported yesterday per Nasdaq; Kalshi already lists next quarter
        ev = EARN + "COST-27JAN05"
        res = self.scan({}, [milestone(ev, et(2027, 1, 5, 17))],
                        nasdaq={"2026-10-04": {"COST": "time-after-hours"}})
        self.assertEqual(self.flagged(res), [])


class TestMilestoneSweep(unittest.TestCase):

    def test_earliest_start_wins_and_only_primary_events_count(self):
        ev, other = EARN + "AAA-26OCT08", EARN + "BBB-26OCT08"
        c = FakeClient([milestone(ev, et(2026, 10, 8, 17)),
                        milestone(ev, et(2026, 10, 8, 10)),
                        milestone(other, et(2026, 10, 8, 9),
                                  primary=False)], page_size=1)
        got = ip.kalshi_event_starts(c, NOW)
        self.assertEqual(got[ev]["start"],
                         et(2026, 10, 8, 10).astimezone(timezone.utc))
        self.assertNotIn(other, got)
        # paged through every row, and swept from NOW -- never a lookback
        sweeps = [p for path, p in c.calls if path == "/milestones"]
        self.assertEqual(len(sweeps), 3)
        self.assertEqual(sweeps[0]["minimum_start_date"], zulu(NOW))

    def test_kalshi_start_for_reads_one_event(self):
        ev = EARN + "AAA-26OCT08"
        c = FakeClient([milestone(ev, et(2026, 10, 1, 10))])
        self.assertEqual(ip.kalshi_start_for(c, ev),
                         et(2026, 10, 1, 10).astimezone(timezone.utc))
        self.assertIsNone(ip.kalshi_start_for(FakeClient([]), ev))
        self.assertIsNone(ip.kalshi_start_for(FakeClient([], boom=True), ev))


class TestNeverRaises(unittest.TestCase):

    def test_the_kill_switch_reports_nothing_and_calls_nothing(self):
        c = FakeClient([milestone(EARN + "LLY-26OCT07",
                                  et(2026, 10, 7, 8, 30))])
        old = ip.ENABLED
        ip.ENABLED = False
        try:
            res = ip.scan(c, NOW, meta={}, nasdaq_for_date=lambda d: {})
        finally:
            ip.ENABLED = old
        self.assertEqual((res["rows"], res["error"], c.calls), ([], None, []))

    def test_a_dead_network_is_an_error_not_an_exception(self):
        res = ip.scan(FakeClient([], boom=True), NOW, meta={},
                      nasdaq_for_date=lambda d: {})
        self.assertEqual(res["rows"], [])
        self.assertIn("network down", res["error"])
        self.assertIn("could not run", ip.error_text(res))
        self.assertEqual(ip.text_lines(res, NOW), [])
        self.assertEqual(ip.html_block(res, NOW), "")


class TestRendering(PickoffCase):

    def _rows(self):
        lly, bull = EARN + "LLY-26OCT07", EARN + "BULL-26OCT14"
        return self.scan({lly: et(2026, 10, 5, 7)},
                         [milestone(lly, et(2026, 10, 7, 8, 30),
                                    "Eli Lilly Earnings Call"),
                          milestone(bull, et(2026, 10, 14, 17),
                                    "Webull Earnings Call")],
                         nasdaq={"2026-10-06": {"BULL": "time-not-supplied"}})

    def test_text_names_the_real_time_and_kalshis_time(self):
        text = "\n".join(ip.text_lines(self._rows(), NOW))
        self.assertTrue(text.startswith(ip.HEADER + "S"))
        self.assertIn("Event is:      Mon Oct 05 07:00 ET (before the open", text)
        self.assertIn("Kalshi thinks: Wed Oct 07 08:30 ET", text)
        self.assertIn("Event is:      Tue Oct 06 (no time published; "
                      "Nasdaq calendar)", text)
        self.assertIn("Kalshi thinks: Wed Oct 14 17:00 ET", text)
        # open window first, and the release proxy is never called the call
        self.assertLess(text.index("Eli Lilly"), text.index("Webull"))
        self.assertIn("OPEN NOW", text)
        self.assertIn("call time not confirmed", text)

    def test_text_survives_the_cp1252_task_console(self):
        # the digest logs its text body to a cp1252 stdout; one stray glyph
        # kills the run before the email goes (2026-09-21)
        text = "\n".join(ip.text_lines(self._rows(), NOW))
        text.encode("ascii")

    def test_html_names_both_times(self):
        h = ip.html_block(self._rows(), NOW)
        for s in ("Mon Oct 05 07:00 ET", "Wed Oct 07 08:30 ET",
                  "Tue Oct 06", "Wed Oct 14 17:00 ET", "KALSHI THINKS",
                  "EVENT IS", "OPEN NOW"):
            self.assertIn(s, h)

    def test_nothing_to_say_renders_nothing(self):
        empty = {"rows": [], "kalshi": {}, "checked": 3, "error": None}
        self.assertEqual(ip.text_lines(empty, NOW), [])
        self.assertEqual(ip.html_block(empty, NOW), "")
        self.assertEqual(ip.error_text(empty), "")

    def test_kalshi_note_says_which_way_the_gap_cuts(self):
        ours = et(2026, 10, 6, 7)
        early = ip.kalshi_note(ours, et(2026, 10, 5, 10), False)
        late = ip.kalshi_note(ours, et(2026, 10, 8, 10), True)
        near = ip.kalshi_note(ours, et(2026, 10, 6, 10), False)
        self.assertIn("nothing to pick off", early)
        self.assertIn("Mon Oct 05 10:00 ET", early)
        self.assertIn(ip.HEADER, late)
        self.assertNotIn("pick off", near)
        self.assertEqual(ip.kalshi_note(ours, None, False), "")


class TestAlertOnce(PickoffCase):
    """The overrides task runs 3x/day and emails only NEW windows."""

    def setUp(self):
        super().setUp()
        self.path = os.path.join(tempfile.mkdtemp(prefix="pickseen_"),
                                 "pickoff_seen.json")
        self.ev = EARN + "LLY-26OCT07"

    def _res(self, kalshi_start):
        return self.scan({self.ev: et(2026, 10, 5, 7)},
                         [milestone(self.ev, kalshi_start)])

    def test_a_window_is_new_once(self):
        res = self._res(et(2026, 10, 7, 8, 30))
        self.assertEqual(len(ip.new_rows(res, NOW, self.path, save=True)), 1)
        self.assertEqual(ip.new_rows(res, NOW, self.path, save=True), [])

    def test_not_saving_leaves_it_new(self):
        # a failed send must retry at the next run
        res = self._res(et(2026, 10, 7, 8, 30))
        ip.new_rows(res, NOW, self.path, save=False)
        self.assertEqual(len(ip.new_rows(res, NOW, self.path, save=False)), 1)
        self.assertFalse(os.path.exists(self.path))

    def test_a_moved_kalshi_time_is_news_again(self):
        ip.new_rows(self._res(et(2026, 10, 7, 8, 30)), NOW, self.path, True)
        moved = self._res(et(2026, 10, 8, 8, 30))
        self.assertEqual(len(ip.new_rows(moved, NOW, self.path, True)), 1)

    def test_a_corrupt_seen_file_alerts_rather_than_going_quiet(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        res = self._res(et(2026, 10, 7, 8, 30))
        self.assertEqual(len(ip.new_rows(res, NOW, self.path, True)), 1)

    def test_old_keys_are_forgotten(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"KXOLD|a|b": (NOW - timedelta(days=60)).isoformat()}, f)
        ip.new_rows(self._res(et(2026, 10, 7, 8, 30)), NOW, self.path, True)
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("KXOLD|a|b", json.load(f))


class TestDigestShowsBothTimes(unittest.TestCase):
    """send_imm_digest's cutoff audit, fed the scan's Kalshi starts."""

    CCL = EARN + "CCL-26OCT05"      # ticker Mon Oct 5
    LLY = EARN + "LLY-26OCT07"      # ticker Wed Oct 7

    class _Client:
        def __init__(self, events):
            self.events = events

        def get(self, path, params=None):
            if "incentive_programs" not in path:
                return {}
            return {"incentive_programs": [{
                "incentive_type": "liquidity", "paid_out": False,
                "market_ticker": ev + "-M0",
                "start_date": "2026-10-01T00:00:00Z",
                "end_date": "2026-10-09T00:00:00Z",
                "period_reward": 6000000} for ev in self.events],
                "next_cursor": None}

    def setUp(self):
        import send_imm_digest as sd
        self.sd = sd
        self.old_meta = sd.OVERRIDE_META_PATH
        sd.OVERRIDE_META_PATH = os.path.join(
            tempfile.mkdtemp(prefix="pick_digest_"), "meta.json")
        self.now = datetime(2026, 10, 4, 11, 10, tzinfo=timezone.utc)
        with open(imm.EVENT_OVERRIDES_FILE, "w", encoding="utf-8") as f:
            json.dump({self.CCL: "2026-10-06T07:00:00-04:00",     # LATE
                       self.LLY: "2026-10-05T07:00:00-04:00"}, f)  # EARLY
        imm._file_override_state["mtime"] = 0.0
        self.client = self._Client([self.CCL, self.LLY])

    def tearDown(self):
        self.sd.OVERRIDE_META_PATH = self.old_meta
        with open(imm.EVENT_OVERRIDES_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f)
        imm._file_override_state["mtime"] = 0.0
        imm.load_file_event_overrides()

    def _audit(self, **kw):
        a = self.sd.cutoff_audit(self.client, self.now,
                                 {self.CCL + "-M0": 473}, **kw)
        self.assertIsNone(a["error"])
        return a, {r["event"]: r for r in a["rows"]}

    def test_the_banner_states_kalshis_time_and_ours(self):
        a, rows = self._audit(kalshi={
            self.CCL: et(2026, 10, 5, 10).astimezone(timezone.utc)})
        banner = self.sd.cutoff_banner(a)
        self.assertIn("Kalshi thinks the EARNINGSMENTIONCCL-26OCT05 call "
                      "starts TOMORROW, Mon Oct 05 10:00 ET", banner)
        self.assertIn("our time is Tue Oct 06 07:00 ET", banner)
        self.assertIn("473 contracts", banner)
        why, _act = self.sd._cutoff_meaning(rows[self.CCL])
        self.assertIn("nothing to pick off", why)

    def test_a_pickoff_row_points_at_the_block(self):
        _a, rows = self._audit(
            kalshi={self.LLY: et(2026, 10, 7, 8, 30).astimezone(timezone.utc)},
            pick_events=[self.LLY])
        why, _act = self.sd._cutoff_meaning(rows[self.LLY])
        self.assertIn("AFTER the real one", why)
        self.assertIn(ip.HEADER, why)
        self.assertIn("Wed Oct 07 08:30 ET", self.sd._cutoff_audit_html(_a))

    def test_without_kalshi_starts_the_old_wording_stands(self):
        a, rows = self._audit()
        self.assertIn("Kalshi says the EARNINGSMENTIONCCL-26OCT05 call is "
                      "TOMORROW (2026-10-05)", self.sd.cutoff_banner(a))
        self.assertIsNone(rows[self.CCL]["kalshi_start"])
        why, _act = self.sd._cutoff_meaning(rows[self.CCL])
        self.assertNotIn("pick off", why)


if __name__ == "__main__":
    unittest.main()
