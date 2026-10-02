"""Tests for treasury_fair (the Treasury touch gate's live yields + fairs)."""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import treasury_fair as tf

ET = tf.ET


def et(y, mo, d, hh, mm):
    return ET.localize(datetime(y, mo, d, hh, mm)).astimezone(timezone.utc)


def cnbc_js(y10=5.26, t=None, extra=True):
    t = t or "2026-10-01T21:01:45.000-0400"
    rows = [{"symbol": "US10Y", "last": f"{y10}%", "last_time": t,
             "curmktstatus": "REG_MKT", "realTime": "true"}]
    if extra:
        rows += [{"symbol": "US30Y", "last": "5.633%", "last_time": t,
                  "curmktstatus": "REG_MKT", "realTime": "true"},
                 {"symbol": "US10Y2", "last": "1%", "last_time": t},       # unknown symbol
                 {"symbol": "US2Y", "last": "n/a", "last_time": t}]        # unparseable
    return {"FormattedQuoteResult": {"FormattedQuote": rows}}


class TestParsing(unittest.TestCase):
    def test_series_strike_and_window(self):
        self.assertEqual(tf.touch_series("KX10YRDIRLM"), (10, "L"))
        self.assertEqual(tf.touch_series("KX30YRDIRHM"), (30, "H"))
        self.assertEqual(tf.touch_series("KX10YRDIRHW"), (10, "H"))
        for s in ("KXUST10AD", "KX10YRDIRLMX", "KX3YRDIRLM", "KX10YRRATE15M"):
            self.assertIsNone(tf.touch_series(s), s)
        self.assertEqual(tf.strike_of("KX10YRDIRLM-26OCT30L-T5.28"), 5.28)
        self.assertIsNone(tf.strike_of("KX10YRDIRLM-26OCT30L-B5.28"))
        self.assertEqual(tf.last_day_of("KX10YRDIRLM-26OCT30L"), date(2026, 10, 30))
        close = datetime(2026, 10, 30, 19, 30, tzinfo=timezone.utc)   # 15:30 ET
        self.assertEqual(tf.last_day_of("KX10YRDIRLM-XYZ", close), date(2026, 10, 30))
        self.assertIsNone(tf.last_day_of("KX10YRDIRLM-XYZ"))

    def test_parse_cnbc(self):
        q = tf.parse_cnbc(cnbc_js())
        self.assertEqual(sorted(q), [10, 30])
        self.assertAlmostEqual(q[10]["y"], 5.26)
        self.assertEqual(q[10]["ts"], datetime(2026, 10, 2, 1, 1, 45, tzinfo=timezone.utc).timestamp())
        self.assertTrue(q[10]["realtime"])
        self.assertEqual(tf.parse_cnbc({}), {})

    def test_fetch_raises_on_an_empty_reply(self):
        r = mock.Mock()
        r.json.return_value = {"FormattedQuoteResult": {"FormattedQuote": []}}
        r.raise_for_status.return_value = None
        with mock.patch.object(tf.requests, "get", return_value=r) as g:
            with self.assertRaises(RuntimeError):
                tf.fetch_cnbc()
        self.assertIn("US10Y", g.call_args.kwargs["params"]["symbols"])
        self.assertNotIn("@", str(g.call_args.kwargs["headers"]))     # no contact email

    def test_sigma_from_par_csv(self):
        lines = ["Date,1 Mo,2 Yr,5 Yr,7 Yr,10 Yr,30 Yr"]
        d = date(2026, 6, 1)
        for i in range(70):
            v = 5.0 + (0.05 if i % 2 else 0.0)          # alternate +-5bp
            lines.append(f"{(d + timedelta(days=i)).strftime('%m/%d/%Y')},4,{v},{v},{v},{v},{v}")
        sig = tf.sigma_from_par_csv(["\n".join(lines)], days=60)
        self.assertEqual(sorted(sig), [2, 5, 7, 10, 30])
        self.assertAlmostEqual(sig[10], 0.05, places=2)
        self.assertEqual(tf.sigma_from_par_csv(["Date,10 Yr\n01/02/2026,5"]), {})


class TestSchedule(unittest.TestCase):
    def test_fixes_skip_weekends_and_holidays(self):
        # Thu 10/1 10:00 ET -> today's fix (frac of a day), Fri 10/2, Mon 10/5 ..
        pending, steps = tf.fix_schedule(et(2026, 10, 1, 10, 0), date(2026, 10, 13))
        self.assertFalse(pending)
        # 10/1,2,5,6,7,8,9,13 (10/12 Columbus Day closed)
        self.assertEqual(len(steps), 8)
        self.assertAlmostEqual(steps[0], 5.5 / 24, places=3)
        self.assertEqual(steps[1:], [1.0] * 7)
        # after the 15:30 fix: today's is PENDING, the next is tomorrow
        pending, steps = tf.fix_schedule(et(2026, 10, 1, 16, 0), date(2026, 10, 2))
        self.assertTrue(pending)
        self.assertEqual(len(steps), 1)
        self.assertAlmostEqual(steps[0], 23.5 / 24, places=3)
        # Saturday: no pending fix, Monday's counts a whole day
        pending, steps = tf.fix_schedule(et(2026, 10, 3, 12, 0), date(2026, 10, 6))
        self.assertFalse(pending)
        self.assertEqual(steps, [1.0, 1.0])
        # the window is over
        self.assertEqual(tf.fix_schedule(et(2026, 10, 30, 16, 0), date(2026, 10, 29)), (False, []))

    def test_windows_and_ttls(self):
        self.assertTrue(tf._in_windows(et(2026, 10, 2, 8, 30), "08:25-08:45"))
        self.assertFalse(tf._in_windows(et(2026, 10, 2, 8, 45), "08:25-08:45"))
        self.assertFalse(tf._in_windows(et(2026, 10, 3, 8, 30), "08:25-08:45"))  # Saturday
        self.assertEqual(tf.quote_ttl_min(et(2026, 10, 2, 9, 0)), tf.QUOTE_TTL_SESSION_MIN)
        self.assertEqual(tf.quote_ttl_min(et(2026, 10, 1, 21, 0)), tf.QUOTE_TTL_OFF_MIN)
        self.assertIsNone(tf.quote_ttl_min(et(2026, 10, 2, 18, 0)))    # Fri after 17:00
        self.assertIsNone(tf.quote_ttl_min(et(2026, 10, 4, 12, 0)))    # Sunday daytime
        self.assertEqual(tf.quote_ttl_min(et(2026, 10, 4, 19, 0)), tf.QUOTE_TTL_OFF_MIN)
        self.assertIsNone(tf.quote_ttl_min(et(2026, 10, 12, 12, 0)))   # Columbus Day
        self.assertEqual(tf.poll_secs(et(2026, 10, 2, 9, 0)), tf.POLL_SECS)
        self.assertEqual(tf.poll_secs(et(2026, 10, 3, 9, 0)), tf.POLL_SECS_CLOSED)


class TestFair(unittest.TestCase):
    def test_touch_probabilities(self):
        sig = 0.06
        # a low strike already crossed with today's fix an hour away: ~certain
        ext = tf.extremes(5.20, [1 / 24, 1, 1], sig, "L")
        self.assertGreater(tf.p_touch(ext, 5.25, "L"), 0.95)
        # far below with three fixes left: ~never
        self.assertLess(tf.p_touch(ext, 4.80, "L"), 0.01)
        # monotone in the strike; high mirrors low
        ps = [tf.p_touch(ext, k, "L") for k in (5.10, 5.15, 5.20, 5.25)]
        self.assertEqual(ps, sorted(ps))
        ext_h = tf.extremes(5.20, [1 / 24, 1, 1], sig, "H")
        self.assertAlmostEqual(tf.p_touch(ext_h, 5.30, "H"), tf.p_touch(ext, 5.10, "L"), delta=0.03)
        # the 2-dp rule: a fix of exactly K is NOT below K
        flat = tf.extremes(5.20, [], sig, "L", pending_y=5.20, basis_sd=0.0)
        self.assertEqual(tf.p_touch(flat, 5.20, "L"), 0.0)
        self.assertEqual(tf.p_touch(flat, 5.21, "L"), 1.0)
        self.assertIsNone(tf.extremes(5.2, [], sig, "L"))
        # fixed seed: the same input gives the same fair
        self.assertTrue((tf.extremes(5.2, [1, 1], sig, "L") == tf.extremes(5.2, [1, 1], sig, "L")).all())


class TestVerdict(unittest.TestCase):
    """YieldWatch.verdict on Fri 2026-10-02 (a weekday) at 13:00 ET."""

    NOW = et(2026, 10, 2, 13, 0)
    CLOSE = datetime(2026, 10, 30, 19, 30, tzinfo=timezone.utc)

    def _watch(self, y10=5.26, quote_age_s=30, read_ts=None):
        w = tf.YieldWatch()
        now_ts = self.NOW.timestamp()
        w.set_sigma({10: 0.0467, 30: 0.0418}, "2026-10-02")
        w.update({10: {"y": y10, "ts": now_ts - quote_age_s, "status": "REG_MKT",
                       "realtime": True}}, read_ts if read_ts is not None else now_ts)
        return w

    def _r(self, w, k, bid=None, ask=None, now=None, series="KX10YRDIRLM"):
        now = now or self.NOW
        return w.verdict(series, f"{series}-26OCT30L-T{k:.2f}", now.timestamp(),
                         bid, ask, self.CLOSE)

    def test_reasons(self):
        w = self._watch()
        self.assertEqual(self._r(w, 5.25)[1]["reason"], "near")      # 5.26 vs 5.245
        self.assertEqual(self._r(w, 5.40)[1]["reason"], "decided")   # 5.26 already below
        why, inp = self._r(w, 5.10, bid=40, ask=60)
        self.assertEqual((why, inp), ("", {}))                         # fair ~45c, book agrees
        why, inp = self._r(w, 5.10, bid=30, ask=32)                   # ask far under fair
        self.assertEqual(inp["reason"], "band")
        self.assertTrue(inp["ask_bad"])
        # a tenor with no quote, a read gone stale, a stale tenor quote
        self.assertEqual(self._r(w, 5.50, series="KX30YRDIRLM")[1]["reason"], "no_quote")
        later = self.NOW + timedelta(seconds=tf.READ_TTL_SECS + 5)
        self.assertEqual(self._r(w, 5.10, 40, 60, now=later)[1]["reason"], "stale_read")
        old = self._watch(quote_age_s=tf.QUOTE_TTL_SESSION_MIN * 60 + 5)
        self.assertEqual(self._r(old, 5.10, 40, 60)[1]["reason"], "stale_quote")
        # the 08:30 release window
        w2 = self._watch(read_ts=et(2026, 10, 2, 8, 30).timestamp())
        self.assertEqual(self._r(w2, 5.10, 40, 60, now=et(2026, 10, 2, 8, 30))[1]["reason"],
                         "release")
        # unparseable
        self.assertEqual(w.verdict("KX10YRDIRLM", "KX10YRDIRLM-26OCT30L-X5.1",
                                   self.NOW.timestamp(), None, None, self.CLOSE)[1]["reason"],
                         "ticker")

    def test_a_fast_move_freezes_the_tenor(self):
        w = self._watch(y10=5.30)
        t0 = self.NOW.timestamp()
        notes = w.update({10: {"y": 5.27, "ts": t0 + 60, "status": "REG_MKT",
                               "realtime": True}}, t0 + 60)
        self.assertEqual(len(notes), 1)
        self.assertIn("US10Y moved 3.0bp", notes[0])
        r = w.verdict("KX10YRDIRLM", "KX10YRDIRLM-26OCT30L-T5.00", t0 + 70, 20, 40, self.CLOSE)
        self.assertEqual(r[1]["reason"], "moving")
        # a further small move inside the freeze: no second note
        self.assertEqual(w.update({10: {"y": 5.268, "ts": t0 + 80, "status": "REG_MKT",
                                        "realtime": True}}, t0 + 80), [])

    def test_pending_fix_after_1530(self):
        # 15:29:50 read at 5.18 becomes today's pending fix; at 16:00 the live
        # yield is back at 5.26 but the low strike 5.21 is ~decided YES
        w = tf.YieldWatch()
        w.set_sigma({10: 0.0467}, "2026-10-02")
        t_fix = et(2026, 10, 2, 15, 29).timestamp() + 50
        w.update({10: {"y": 5.18, "ts": t_fix, "status": "REG_MKT", "realtime": True}}, t_fix)
        t16 = et(2026, 10, 2, 16, 0).timestamp()
        w.update({10: {"y": 5.26, "ts": t16, "status": "REG_MKT", "realtime": True}}, t16)
        p = w.fair(10, "L", 5.21, date(2026, 10, 30), et(2026, 10, 2, 16, 0))
        self.assertGreater(p, 0.95)
        # too long a window has no fair
        self.assertIsNone(w.fair(10, "L", 5.21, date(2027, 6, 30), et(2026, 10, 2, 16, 0)))


if __name__ == "__main__":
    unittest.main()
