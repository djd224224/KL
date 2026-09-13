#!/usr/bin/env python3
"""Unit tests for send_imm_new_programs.py — the daily "new incentive
programs" email (Jack 2026-09-13: "new daily morning email, table of all the
new incentive rewards events that started in the past day").

Run: python -m unittest test_send_imm_new_programs

What is worth testing here is NOT the HTML — it is the two ways this email
can lie:
  * report an old event as new (the program-ROLL case: a second period opens
    on an event that has been paying for weeks, which is what would happen if
    "new" were decided on start_date alone), and
  * silently drop a real one (a day the send fails must widen the next
    window, and a collapsed feed must not advance the watermark).
Plus the money math: period_reward is CENTI-CENTS and an hourly program's
$/day is a rate, both of which are easy to get wrong by 100x.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import send_imm_new_programs as np                                  # noqa: E402
import incentive_mm as imm                                          # noqa: E402

NOW = datetime(2026, 9, 13, 11, 30, tzinfo=timezone.utc)            # 7:30 AM ET


def setUpModule():
    """Sandbox every file path away from the live run-logs directory, and make
    sure no test can SMTP the user (the trading box has live creds in env)."""
    tmp = tempfile.mkdtemp(prefix="imm_newprog_test_")
    imm.STATUS_DIR = tmp
    imm.ALERT_RECIPIENTS = []
    imm.EVENT_OVERRIDES_FILE = os.path.join(tmp, "event_start_overrides.json")
    imm.EXTRA_ALLOW_FILE = os.path.join(tmp, "extra_allow_series.json")
    imm.FINECON_EXTRA_FILE = os.path.join(tmp, "finecon_extra_series.json")
    np.SEEN_PATH = os.path.join(tmp, "imm_new_programs_seen.json")
    np.STATE_PATH = os.path.join(tmp, "imm_state.json")
    # Fixture series are not in the production allowlist; allowlist policy has
    # its own tests below that patch _allowed/_blocked explicitly.
    imm.ALLOWLIST_ONLY = False


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prog(ticker, start, end, reward=1000000, target=1000, itype="liquidity",
         paid=False, pid=None):
    """One /incentive_programs row. reward is CENTI-CENTS (1,000,000 = $100)."""
    return {"id": pid or f"{ticker}:{_iso(start)}", "market_ticker": ticker,
            "start_date": _iso(start), "end_date": _iso(end),
            "period_reward": reward, "target_size_fp": target,
            "discount_factor_bps": 5000, "incentive_type": itype,
            "paid_out": paid}


class FakeClient:
    """Pages /incentive_programs and answers /events/<ticker> titles."""

    def __init__(self, rows, page_size=1000, titles=None):
        self.rows = list(rows)
        self.page_size = page_size
        self.titles = titles or {}
        self.calls = []

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, dict(params)))
        if path == "/incentive_programs":
            rows = [r for r in self.rows
                    if params.get("status") in (None, "active")]
            start = int(params.get("cursor") or 0)
            page = rows[start:start + self.page_size]
            nxt = start + self.page_size
            return {"incentive_programs": page,
                    "next_cursor": str(nxt) if nxt < len(rows) else None}
        if path.startswith("/events/"):
            return {"event": {"title": self.titles.get(path.split("/")[-1], "")}}
        return {}


def write_seen(rec):
    with open(np.SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(rec, f)


def clear_files():
    for p in (np.SEEN_PATH, np.STATE_PATH):
        if os.path.exists(p):
            os.remove(p)
    for name in os.listdir(imm.STATUS_DIR):
        if name.startswith("imm_new_programs_sent_"):
            os.remove(os.path.join(imm.STATUS_DIR, name))
    gaps_cache = getattr(np.gaps, "_TITLE_CACHE", None)
    if isinstance(gaps_cache, dict):
        gaps_cache.clear()


def run_report(rows, now=NOW, **kw):
    client = FakeClient(rows)
    with mock.patch.object(imm, "build_client", return_value=client):
        return np.build_report(now, **kw) + (client,)


# ---------------------------------------------------------------------------
# Money math and roll-up
# ---------------------------------------------------------------------------

class RollUp(unittest.TestCase):
    def test_centi_cents_to_dollars_and_per_day(self):
        """1,000,000 centi-cents = $100; over 2 days that is $50/day."""
        rows = [prog("KXA-26SEP20-T1", NOW - timedelta(days=1),
                     NOW + timedelta(days=1), reward=1000000)]
        events, _ = np.roll_up_events(rows, NOW)
        rec = events["KXA-26SEP20"]
        self.assertAlmostEqual(rec["pool_total"], 100.0, places=6)
        self.assertAlmostEqual(rec["pool_day"], 50.0, places=6)

    def test_hourly_program_rate_is_floored_at_one_hour(self):
        """A 1h program worth $10 is $240/day, not a division blow-up."""
        rows = [prog("KXTEMPMIAH-26SEP13-T80", NOW, NOW + timedelta(hours=1),
                     reward=100000)]
        events, _ = np.roll_up_events(rows, NOW)
        self.assertAlmostEqual(events["KXTEMPMIAH-26SEP13"]["pool_day"],
                               240.0, places=6)

    def test_event_aggregates_markets_and_overlapping_programs(self):
        rows = [
            prog("KXB-26SEP20-T1", NOW, NOW + timedelta(days=1), reward=500000),
            prog("KXB-26SEP20-T2", NOW, NOW + timedelta(days=1), reward=500000),
            # a second program joining the SAME market late — both pay
            prog("KXB-26SEP20-T1", NOW + timedelta(hours=6),
                 NOW + timedelta(days=2), reward=200000, target=2000,
                 pid="second"),
        ]
        events, _ = np.roll_up_events(rows, NOW)
        rec = events["KXB-26SEP20"]
        self.assertEqual(len(rec["tickers"]), 2)
        self.assertEqual(rec["n_programs"], 3)
        self.assertAlmostEqual(rec["pool_total"], 120.0, places=6)
        self.assertEqual(rec["start"], NOW)                 # earliest
        self.assertEqual(rec["end"], NOW + timedelta(days=2))   # latest
        self.assertEqual(rec["target"], 2000)               # max depth ask

    def test_non_liquidity_is_skipped_but_counted(self):
        rows = [prog("KXC-26SEP20-T1", NOW, NOW + timedelta(days=1)),
                prog("KXC-26SEP20-T2", NOW, NOW + timedelta(days=1),
                     itype="volume")]
        events, skipped = np.roll_up_events(rows, NOW)
        self.assertEqual(len(events["KXC-26SEP20"]["tickers"]), 1)
        self.assertEqual(skipped["volume"], 1)

    def test_ended_and_paid_out_programs_are_not_live(self):
        rows = [prog("KXD-26SEP12-T1", NOW - timedelta(hours=20),
                     NOW - timedelta(hours=2)),
                prog("KXD-26SEP12-T2", NOW - timedelta(hours=20),
                     NOW + timedelta(hours=2), paid=True)]
        events, _ = np.roll_up_events(rows, NOW)
        rec = events["KXD-26SEP12"]
        self.assertEqual(rec["live_tickers"], set())
        self.assertIn("ended", np.fmt_window(rec, NOW))

    def test_pagination_follows_the_cursor(self):
        rows = [prog(f"KXE-26SEP20-T{i}", NOW, NOW + timedelta(days=1))
                for i in range(5)]
        client = FakeClient(rows, page_size=2)
        got = np.fetch_programs_raw(client)
        self.assertEqual(len(got), 5)
        self.assertEqual(sum(1 for c in client.calls
                             if c[0] == "/incentive_programs"), 3)

    def test_live_feed_read_failure_propagates(self):
        """A half-paged feed emailed as the truth is worse than a late email:
        the caller's retry loop must see the error."""
        class Broken:
            def get(self, path, params=None):
                raise RuntimeError("502")
        with self.assertRaises(RuntimeError):
            np.fetch_programs_raw(Broken())

    def test_optional_status_failure_is_tolerated(self):
        """--include-ended must never cost the whole email."""
        rows = [prog("KXE-26SEP20-T1", NOW, NOW + timedelta(days=1))]

        class OnlyActive(FakeClient):
            def get(self, path, params=None):
                if (params or {}).get("status") not in (None, "active"):
                    raise RuntimeError("unsupported status")
                return FakeClient.get(self, path, params)

        got = np.fetch_programs_raw(OnlyActive(rows), ("active", "settled"))
        self.assertEqual(len(got), 1)

    def test_duplicate_rows_across_statuses_are_deduped(self):
        row = prog("KXE-26SEP20-T1", NOW, NOW + timedelta(days=1))

        class Both(FakeClient):
            def get(self, path, params=None):
                return {"incentive_programs": [row], "next_cursor": None}

        got = np.fetch_programs_raw(Both([row]), ("active", "settled"))
        self.assertEqual(len(got), 1)


# ---------------------------------------------------------------------------
# What counts as NEW
# ---------------------------------------------------------------------------

class NewDetection(unittest.TestCase):
    def setUp(self):
        clear_files()

    def test_first_run_reports_only_in_window_starts(self):
        rows = [prog("KXOLD-26SEP20-T1", NOW - timedelta(days=6),
                     NOW + timedelta(days=4), reward=1000000),
                prog("KXNEW-26SEP20-T1", NOW - timedelta(hours=3),
                     NOW + timedelta(days=2), reward=2000000)]
        text, _html, subject, events, seen_next, _c = run_report(rows)
        self.assertIn("NEW-26SEP20", text)
        self.assertNotIn("OLD-26SEP20", text.split("Feed now")[0])
        self.assertIn("1 event,", subject)
        # both are recorded, so neither can be "new" again tomorrow
        self.assertEqual(set(seen_next["events"]), set(events))

    def test_program_roll_on_a_known_event_is_not_new(self):
        """The case start_date alone gets wrong: yesterday's period ended, a
        fresh one opened this morning, and the event has been paying for
        weeks. It is not new; it must not appear."""
        write_seen({"version": 1,
                    "watermark": _iso(NOW - timedelta(days=1)),
                    "events": {"KXROLL-26SEP20": {
                        "first_seen": _iso(NOW - timedelta(days=14)),
                        "start": _iso(NOW - timedelta(days=14)),
                        "last_seen": _iso(NOW - timedelta(days=1)),
                        "series": "KXROLL"}},
                    "series": {"KXROLL": {
                        "first_seen": _iso(NOW - timedelta(days=14)),
                        "last_seen": _iso(NOW - timedelta(days=1))}}})
        rows = [prog("KXROLL-26SEP20-T1", NOW - timedelta(hours=4),
                     NOW + timedelta(days=1))]
        text, _html, subject, _events, _seen, _c = run_report(rows)
        self.assertIn("No new incentive-reward events", text)
        self.assertIn("0 events", subject)

    def test_genuinely_new_event_on_a_known_series_is_reported(self):
        write_seen({"version": 1,
                    "watermark": _iso(NOW - timedelta(days=1)),
                    "events": {"KXROLL-26SEP20": {
                        "first_seen": _iso(NOW - timedelta(days=14)),
                        "start": _iso(NOW - timedelta(days=14)),
                        "last_seen": _iso(NOW - timedelta(days=1)),
                        "series": "KXROLL"}},
                    "series": {"KXROLL": {
                        "first_seen": _iso(NOW - timedelta(days=14)),
                        "last_seen": _iso(NOW - timedelta(days=1))}}})
        rows = [prog("KXROLL-26SEP20-T1", NOW - timedelta(days=14),
                     NOW + timedelta(days=1)),
                prog("KXROLL-26SEP27-T1", NOW - timedelta(hours=2),
                     NOW + timedelta(days=7), reward=3000000)]
        text, _html, _subject, _events, _seen, _c = run_report(rows)
        self.assertIn("ROLL-26SEP27", text)
        head = text.split("Feed now")[0]
        self.assertNotIn("ROLL-26SEP20", head)

    def test_unknown_event_that_started_before_the_window_is_a_late_arrival(self):
        write_seen({"version": 1,
                    "watermark": _iso(NOW - timedelta(hours=24)),
                    "events": {"KXKNOWN-26SEP20": {
                        "first_seen": _iso(NOW - timedelta(days=3)),
                        "start": _iso(NOW - timedelta(days=3)),
                        "last_seen": _iso(NOW - timedelta(hours=24)),
                        "series": "KXKNOWN"}},
                    "series": {}})
        rows = [prog("KXKNOWN-26SEP20-T1", NOW - timedelta(days=3),
                     NOW + timedelta(days=3)),
                prog("KXLATE-26SEP20-T1", NOW - timedelta(days=3),
                     NOW + timedelta(days=3), reward=4000000)]
        text, _html, subject, _events, _seen, _c = run_report(rows)
        self.assertIn("0 events", subject)
        self.assertIn("LATE ARRIVALS", text)
        self.assertIn("LATE-26SEP20", text)

    def test_new_series_is_called_out(self):
        write_seen({"version": 1,
                    "watermark": _iso(NOW - timedelta(hours=24)),
                    "events": {"KXKNOWN-26SEP20": {
                        "first_seen": _iso(NOW - timedelta(days=3)),
                        "start": _iso(NOW - timedelta(days=3)),
                        "last_seen": _iso(NOW - timedelta(hours=24)),
                        "series": "KXKNOWN"}},
                    "series": {"KXKNOWN": {
                        "first_seen": _iso(NOW - timedelta(days=3)),
                        "last_seen": _iso(NOW - timedelta(hours=24))}}})
        rows = [prog("KXAVGTKDFW-26SEP20-T1", NOW - timedelta(hours=5),
                     NOW + timedelta(days=7), reward=5000000)]
        text, _html, _subject, _events, _seen, _c = run_report(rows)
        self.assertIn("NEW SERIES: KXAVGTKDFW", text)

    def test_relit_series_is_called_out(self):
        """KXTEMPMIAH 2026-08-15: a family Kalshi had gone dark on comes back.
        The event is new, the SERIES is not — and the relight is the signal."""
        write_seen({"version": 1,
                    "watermark": _iso(NOW - timedelta(hours=24)),
                    "events": {},
                    "series": {"KXTEMPMIAH": {
                        "first_seen": _iso(NOW - timedelta(days=60)),
                        "last_seen": _iso(NOW - timedelta(days=16))}}})
        rows = [prog("KXTEMPMIAH-26SEP1307-T80", NOW - timedelta(minutes=40),
                     NOW + timedelta(minutes=20), reward=800000)]
        text, _html, _subject, _events, _seen, _c = run_report(rows)
        self.assertIn("RELIT SERIES: KXTEMPMIAH", text)
        self.assertIn("dark 16d", text)

    def test_empty_feed_raises_so_the_caller_retries(self):
        with self.assertRaises(RuntimeError):
            run_report([])


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

class Window(unittest.TestCase):
    def test_no_watermark_uses_the_default_lookback(self):
        start, why = np.window_start_for({}, NOW)
        self.assertEqual(start, NOW - timedelta(hours=np.LOOKBACK_HOURS))
        self.assertIn("no previous email", why)

    def test_watermark_is_the_window_start(self):
        wm = NOW - timedelta(hours=26)
        start, why = np.window_start_for({"watermark": _iso(wm)}, NOW)
        self.assertEqual(start, wm)
        self.assertIn("since the last email", why)

    def test_a_long_outage_is_capped(self):
        wm = NOW - timedelta(days=30)
        start, why = np.window_start_for({"watermark": _iso(wm)}, NOW)
        self.assertEqual(start, NOW - timedelta(hours=np.MAX_LOOKBACK_HOURS))
        self.assertIn("capped", why)

    def test_future_watermark_falls_back(self):
        wm = NOW + timedelta(days=2)
        start, _why = np.window_start_for({"watermark": _iso(wm)}, NOW)
        self.assertEqual(start, NOW - timedelta(hours=np.LOOKBACK_HOURS))

    def test_since_override(self):
        start, why = np.window_start_for({"watermark": _iso(NOW)}, NOW,
                                         since_hours=72)
        self.assertEqual(start, NOW - timedelta(hours=72))
        self.assertIn("72h", why)

    def test_a_failed_send_widens_the_next_window(self):
        """A send that fails writes no watermark, so the next day's window
        still reaches back to the last email Jack actually got and yesterday's
        event is reported then instead of falling in a hole."""
        clear_files()
        write_seen({"version": 1, "watermark": _iso(NOW - timedelta(hours=24)),
                    "feed_programs": 1, "events": {}, "series": {}})
        rows = [prog("KXMISS-26SEP20-T1", NOW - timedelta(hours=3),
                     NOW + timedelta(days=3), reward=1000000)]
        text, _h, _s, _e, _seen, _c = run_report(rows)
        self.assertIn("MISS-26SEP20", text)
        # main() writes the seen-file only after a successful send, so the
        # watermark on disk is still yesterday's
        text2, _h2, _s2, _e2, _seen2, _c2 = run_report(
            rows, now=NOW + timedelta(hours=24))
        self.assertIn("MISS-26SEP20", text2)
        self.assertIn("48.0h", text2)          # window widened, nothing lost


class FeedHealth(unittest.TestCase):
    def setUp(self):
        clear_files()

    def test_collapsed_feed_warns_and_holds_the_watermark(self):
        old_wm = _iso(NOW - timedelta(days=1))
        write_seen({"version": 1, "watermark": old_wm, "feed_programs": 4000,
                    "events": {}, "series": {}})
        rows = [prog("KXF-26SEP20-T1", NOW - timedelta(hours=2),
                     NOW + timedelta(days=1))]
        text, _html, _subject, _events, seen_next, _c = run_report(rows)
        self.assertIn("FEED SHRANK", text)
        self.assertEqual(imm.parse_iso_utc(seen_next["watermark"]),
                         imm.parse_iso_utc(old_wm))
        self.assertFalse(seen_next["feed_ok"])

    def test_healthy_feed_advances_the_watermark(self):
        write_seen({"version": 1, "watermark": _iso(NOW - timedelta(days=1)),
                    "feed_programs": 2, "events": {}, "series": {}})
        rows = [prog("KXF-26SEP20-T1", NOW - timedelta(hours=2),
                     NOW + timedelta(days=1)),
                prog("KXF-26SEP20-T2", NOW - timedelta(hours=2),
                     NOW + timedelta(days=1))]
        _t, _h, _s, _e, seen_next, _c = run_report(rows)
        self.assertEqual(seen_next["watermark"], NOW.isoformat())
        self.assertTrue(seen_next["feed_ok"])

    def test_stale_seen_entries_are_pruned(self):
        old = _iso(NOW - timedelta(days=np.KEEP_DAYS + 5))
        seen = {"version": 1, "watermark": _iso(NOW - timedelta(days=1)),
                "events": {"KXGONE-25JAN01": {"first_seen": old, "start": old,
                                              "last_seen": old,
                                              "series": "KXGONE"}},
                "series": {"KXGONE": {"first_seen": old, "last_seen": old}}}
        events = {}
        out = np.next_seen(seen, events, NOW, True)
        self.assertNotIn("KXGONE-25JAN01", out["events"])
        self.assertNotIn("KXGONE", out["series"])

    def test_seen_entries_keep_first_seen_and_nothing_else(self):
        """The file holds four months of a feed that lists thousands of events
        a day, so an entry is two timestamps — and first_seen must survive a
        re-record or every event would look new again."""
        first = _iso(NOW - timedelta(days=3))
        seen = {"version": 1, "watermark": _iso(NOW - timedelta(days=1)),
                "events": {"KXA-26SEP20": {"first_seen": first,
                                           "last_seen": _iso(NOW - timedelta(days=1))}},
                "series": {}}
        rows = [prog("KXA-26SEP20-T1", NOW - timedelta(days=3),
                     NOW + timedelta(days=1))]
        events, _ = np.roll_up_events(rows, NOW)
        out = np.next_seen(seen, events, NOW, True)
        self.assertEqual(out["events"]["KXA-26SEP20"],
                         {"first_seen": first, "last_seen": NOW.isoformat()})

    def test_corrupt_seen_entries_do_not_raise(self):
        seen = {"version": 1, "watermark": _iso(NOW - timedelta(days=1)),
                "events": {"KXA-26SEP20": "not a dict"},
                "series": {"KXA": 17}}
        rows = [prog("KXA-26SEP20-T1", NOW - timedelta(hours=2),
                     NOW + timedelta(days=1))]
        events, _ = np.roll_up_events(rows, NOW)
        out = np.next_seen(seen, events, NOW, True)
        self.assertEqual(out["events"]["KXA-26SEP20"]["first_seen"],
                         NOW.isoformat())

    def test_unreadable_seen_file_is_treated_as_a_first_run(self):
        clear_files()
        with open(np.SEEN_PATH, "w", encoding="utf-8") as f:
            f.write("{not json")
        rows = [prog("KXA-26SEP20-T1", NOW - timedelta(hours=2),
                     NOW + timedelta(days=1))]
        text, _h, _s, _e, _seen, _c = run_report(rows)
        self.assertIn("A-26SEP20", text)


# ---------------------------------------------------------------------------
# The BOT column
# ---------------------------------------------------------------------------

class BotColumn(unittest.TestCase):
    def _rec(self, event="KXZ-26SEP20", tickers=("KXZ-26SEP20-T1",),
             start=None):
        return {"event": event, "series": imm.series_of(event + "-T1"),
                "tickers": set(tickers), "live_tickers": set(tickers),
                "pool_day": 1.0, "pool_total": 1.0,
                "start": start or (NOW - timedelta(hours=2)),
                "end": NOW + timedelta(days=1), "target": 1000.0,
                "dfs": {0.5}, "n_programs": len(tickers)}

    def test_quoting_count(self):
        rec = self._rec(tickers=("KXZ-26SEP20-T1", "KXZ-26SEP20-T2"))
        out = np.bot_status(rec, {"KXZ-26SEP20-T1"}, imm.EventStartResolver(), NOW)
        self.assertEqual(out, "quoting 1/2")

    def test_not_allowlisted(self):
        rec = self._rec()
        with mock.patch.object(imm.IncentiveMarketMaker, "_allowed",
                               return_value=False), \
             mock.patch.object(imm.IncentiveMarketMaker, "_blocked",
                               return_value=False):
            self.assertEqual(
                np.bot_status(rec, set(), imm.EventStartResolver(), NOW),
                "not allowlisted")

    def test_blocked_by_config(self):
        rec = self._rec()
        with mock.patch.object(imm.IncentiveMarketMaker, "_allowed",
                               return_value=False), \
             mock.patch.object(imm.IncentiveMarketMaker, "_blocked",
                               return_value=True):
            self.assertEqual(
                np.bot_status(rec, set(), imm.EventStartResolver(), NOW),
                "blocked (config)")

    def test_unearnable_program_starts_after_the_midnight_cutoff(self):
        """The KXTEMPMIAH class: the whole reward period lies after the bot's
        midnight-ET fallback cutoff, so not a cent of it is earnable."""
        rec = self._rec(event="KXZ-26SEP12",
                        tickers=("KXZ-26SEP12-T1",),
                        start=datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc))
        out = np.bot_status(rec, set(), imm.EventStartResolver(), NOW)
        self.assertIn("UNEARNABLE", out)

    def test_cutoff_passed(self):
        rec = self._rec(event="KXZ-26SEP12", tickers=("KXZ-26SEP12-T1",),
                        start=datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc))
        out = np.bot_status(rec, set(), imm.EventStartResolver(), NOW)
        self.assertIn("cutoff passed", out)

    def test_close_anchored_series_is_not_flagged(self):
        """Hourly weather/AQI overrides are governed by close_time; the ticker
        date says nothing, so the feed cannot judge them (same carve-out as
        imm_feed_audit)."""
        rec = self._rec(event="KXTEMPMIAH-26SEP12",
                        tickers=("KXTEMPMIAH-26SEP12-T80",),
                        start=datetime(2026, 9, 12, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(np.cutoff_flag(rec, imm.EventStartResolver(), NOW), "")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

class Rendering(unittest.TestCase):
    def setUp(self):
        clear_files()

    def test_total_row_covers_every_new_event_even_when_rows_are_capped(self):
        rows = []
        for i, reward in enumerate((3000000, 2000000, 1000000)):
            rows.append(prog(f"KXR{i}-26SEP20-T1", NOW - timedelta(hours=2),
                             NOW + timedelta(days=1), reward=reward))
        with mock.patch.object(np, "MAX_ROWS", 2):
            text, html, _s, _e, _seen, _c = run_report(rows)
        # $300 + $200 + $100 over a ~1.08d window -> the TOTAL must be all 3
        self.assertIn("and 1 smaller event(s)", text)
        total_line = [ln for ln in text.splitlines() if ln.startswith("TOTAL")][0]
        self.assertIn("600.00", total_line)          # pool$ across all three
        self.assertIn("R0-26SEP20", html)
        self.assertNotIn("R2-26SEP20", html.split("TOTAL")[0])

    def test_html_and_text_agree_on_the_headline_numbers(self):
        rows = [prog("KXS-26SEP20-T1", NOW - timedelta(hours=2),
                     NOW + timedelta(days=1), reward=1000000)]
        text, html, subject, _e, _seen, _c = run_report(rows)
        self.assertIn("1 new event", text)
        self.assertIn("1 new event", html)
        self.assertIn("$100.00 of new pool", text)
        self.assertIn("1 event,", subject)

    def test_event_title_is_used_for_the_what_column(self):
        rows = [prog("KXWHAT-26SEP20-T1", NOW - timedelta(hours=2),
                     NOW + timedelta(days=1))]
        client = FakeClient(rows, titles={"KXWHAT-26SEP20": "Will it snow?"})
        with mock.patch.object(imm, "build_client", return_value=client):
            text, _html, _s, _e, _seen = np.build_report(NOW)
        self.assertIn("Will it snow?", text)

    def test_non_standard_discount_factor_is_flagged(self):
        """0.5^(ticks behind best) is the whole ladder's premise."""
        row = prog("KXDF-26SEP20-T1", NOW - timedelta(hours=2),
                   NOW + timedelta(days=1))
        row["discount_factor_bps"] = 7500
        text, _html, _s, _e, _seen, _c = run_report([row])
        self.assertIn("NON-STANDARD discount factor", text)
        self.assertIn("0.75", text)

    def test_et_stamp_is_portable(self):
        """%-m/%-I are glibc-only and the live box is Windows."""
        self.assertEqual(np._et(datetime(2026, 9, 13, 0, 11, tzinfo=timezone.utc)),
                         "9/12 8:11pm")
        self.assertEqual(np._et(datetime(2026, 9, 13, 16, 5, tzinfo=timezone.utc)),
                         "9/13 12:05pm")

    def test_body_is_ascii(self):
        """The email's OWN chrome stays ASCII — the plain-text part also goes
        to SMS gateways. (A Kalshi event title may not be; MIMEText falls back
        to utf-8 for those, which is fine.)"""
        rows = [prog("KXT-26SEP20-T1", NOW - timedelta(hours=2),
                     NOW + timedelta(days=1))]
        text, _html, _s, _e, _seen, _c = run_report(rows)
        text.encode("ascii")


# ---------------------------------------------------------------------------
# main(): markers, state writes, dry runs
# ---------------------------------------------------------------------------

class FakeAlerter:
    sent = []
    ok = True

    def __init__(self, tag, live=True):
        self.enabled = True

    def send_message(self, body, subject="", html=None):
        FakeAlerter.sent.append((subject, body, html))
        return FakeAlerter.ok


class MainFlow(unittest.TestCase):
    def setUp(self):
        clear_files()
        FakeAlerter.sent = []
        FakeAlerter.ok = True
        rows = [prog("KXM-26SEP20-T1", datetime.now(timezone.utc) - timedelta(hours=2),
                     datetime.now(timezone.utc) + timedelta(days=1))]
        self.client = FakeClient(rows)

    def _run(self, argv):
        with mock.patch.object(imm, "build_client", return_value=self.client), \
             mock.patch.object(imm, "Alerter", FakeAlerter):
            return np.main(argv)

    def _marker(self):
        today_ct = datetime.now(timezone.utc).astimezone(imm.CT).date()
        return os.path.join(imm.STATUS_DIR,
                            f"imm_new_programs_sent_{today_ct}.marker")

    def test_scheduled_run_sends_writes_marker_and_state(self):
        self.assertEqual(self._run([]), 0)
        self.assertEqual(len(FakeAlerter.sent), 1)
        self.assertTrue(os.path.exists(self._marker()))
        self.assertIn("KXM-26SEP20", json.load(open(np.SEEN_PATH))["events"])

    def test_marker_makes_a_second_run_a_no_op(self):
        self._run([])
        FakeAlerter.sent = []
        self.assertEqual(self._run([]), 0)
        self.assertEqual(FakeAlerter.sent, [])

    def test_test_flag_sends_without_writing_the_marker(self):
        self.assertEqual(self._run(["--test"]), 0)
        self.assertEqual(len(FakeAlerter.sent), 1)
        self.assertFalse(os.path.exists(self._marker()))
        # a --test email is still one Jack saw: the watermark moves
        self.assertTrue(os.path.exists(np.SEEN_PATH))

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        self.assertEqual(self._run(["--dry"]), 0)
        self.assertEqual(FakeAlerter.sent, [])
        self.assertFalse(os.path.exists(np.SEEN_PATH))
        self.assertFalse(os.path.exists(self._marker()))

    def test_failed_send_writes_no_state(self):
        FakeAlerter.ok = False
        with mock.patch.object(np.time, "sleep", lambda *_a: None):
            rc = self._run(["--test"])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(np.SEEN_PATH))
        self.assertFalse(os.path.exists(self._marker()))


if __name__ == "__main__":
    unittest.main()
