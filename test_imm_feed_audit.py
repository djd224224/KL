#!/usr/bin/env python3
"""Unit tests for imm_feed_audit.py -- run: python -m unittest test_imm_feed_audit

Worth testing (Jack 2026-10-11, "yes fix" to the false alarms in the morning
email): UNEARNABLE is a SERIES verdict -- last week's strait-transit event,
whose renewed daily reward period starts after its cutoff, must not flag a
series whose live event is earnable, while a series with no earnable event
(the KXTEMPMIAH class) still is; and a blocked family losing its programs is
not "LEFT the feed".
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import incentive_mm as imm                                          # noqa: E402
import imm_feed_audit as fa                                         # noqa: E402

MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()


def setUpModule():
    """Keep the audit's offline bot away from the live run-logs."""
    tmp = tempfile.mkdtemp(prefix="imm_feed_audit_test_")
    imm.STATUS_DIR = tmp
    imm.HALT_FILE = os.path.join(tmp, "HALT")
    imm.IncentiveMarketMaker.PERSIST_PATH = os.path.join(tmp, "imm_state.json")
    imm.IncentiveMarketMaker.ORDER_JOURNAL_PATH = os.path.join(tmp, "imm_order_journal.jsonl")
    imm.EVENT_OVERRIDES_FILE = os.path.join(tmp, "event_start_overrides.json")
    imm.EXTRA_ALLOW_FILE = os.path.join(tmp, "extra_allow_series.json")


class _NoResolver:
    def __init__(self, *a, **k):
        pass

    def resolve(self, series, event_ticker):
        return None


def _seg(d) -> str:
    return f"{d.year % 100:02d}{MON[d.month - 1]}{d.day:02d}"


def _prog(start, days=1.0):
    return {"start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_date": (start + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "period_reward": 1000000}


class TestUnearnableIsASeriesVerdict(unittest.TestCase):

    def _audit(self, progs):
        with mock.patch.object(imm, "EventStartResolver", _NoResolver):
            return fa.run_audit(progs=progs)

    def test_a_finished_event_with_a_renewed_period_is_not_unearnable(self):
        now = datetime.now(timezone.utc)
        today = now.date()
        old = f"KXHORMUZWEEKLY-{_seg(today - timedelta(days=1))}"
        live = f"KXHORMUZWEEKLY-{_seg(today + timedelta(days=6))}"
        bot = imm.IncentiveMarketMaker(client=None, live=False)
        self.assertTrue(bot._allowed(old + "-T20"))
        # last week's event: Kalshi renewed its period an hour ago, after its
        # midnight cutoff; this week's event has a period the bot can earn
        progs = {f"{old}-T20": _prog(now - timedelta(hours=1)),
                 f"{live}-T20": _prog(now - timedelta(hours=1))}
        a = self._audit(progs)
        self.assertEqual(a["unearnable"], set())
        line = next(l for l in a["miss_lines"] if l.startswith(old))
        self.assertNotIn("UNEARNABLE", line)
        self.assertIn("renewed its program", line)

    def test_a_series_with_no_earnable_event_still_flags(self):
        # the KXTEMPMIAH class: every event's program starts after its cutoff
        now = datetime.now(timezone.utc)
        ev = f"KXTRUMPAPPROVE-{_seg(now.date() - timedelta(days=1))}"
        bot = imm.IncentiveMarketMaker(client=None, live=False)
        self.assertTrue(bot._allowed(ev + "-T45"))
        a = self._audit({f"{ev}-T45": _prog(now - timedelta(hours=1))})
        self.assertEqual(a["unearnable"], {"KXTRUMPAPPROVE"})
        self.assertIn("UNEARNABLE", next(l for l in a["miss_lines"] if l.startswith(ev)))


class TestAbsentSkipsBlockedFamilies(unittest.TestCase):

    def test_a_blocked_family_losing_its_programs_is_not_a_miss(self):
        bot = imm.IncentiveMarketMaker(client=None, live=False)
        overrides = dict(imm.SERIES_OVERRIDES)
        self.addCleanup(lambda: (imm.SERIES_OVERRIDES.clear(),
                                 imm.SERIES_OVERRIDES.update(overrides)))
        imm.SERIES_OVERRIDES["KXBLOCKEDCLOSE"] = imm.SeriesOverride(cutoff_from_close_min=30)
        imm.SERIES_OVERRIDES["KXLIVECLOSE"] = imm.SeriesOverride(cutoff_from_close_min=30)
        real_blocked = bot._blocked
        with mock.patch.object(imm.IncentiveMarketMaker, "_blocked",
                               lambda self, t: t.startswith("KXBLOCKEDCLOSE-")
                               or real_blocked(t)), \
                mock.patch.object(imm, "EventStartResolver", _NoResolver):
            a = fa.run_audit(progs={})
        self.assertIn("KXLIVECLOSE", a["absent"])
        self.assertNotIn("KXBLOCKEDCLOSE", a["absent"])


if __name__ == "__main__":
    unittest.main()
