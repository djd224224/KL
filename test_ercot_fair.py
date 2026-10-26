"""Tests for ercot_fair.py (KXTXERCOTPEAKD, ERCOT's daily peak hourly load).
No network: ERCOT's feed and Kalshi are handed in, and requests.get raises."""
import json
import math
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import ercot_fair as ef

EV = "KXTXERCOTPEAKD-26OCT27"          # operating day 2026-10-26
RULES = ("If the highest hourly total system load reported for the ERCOT grid on "
         "October 26, 2026 is above \n\n{k:,} MW, then the market resolves to Yes.")
SHAPE = [0.80, 0.77, 0.75, 0.74, 0.74, 0.76, 0.79, 0.81, 0.82, 0.85, 0.88, 0.91,
         0.94, 0.97, 0.99, 1.00, 1.00, 0.99, 0.96, 0.95, 0.93, 0.90, 0.86, 0.82]
SHAPE[16] = 1.0                       # HE17 is the forecast peak
SHAPE[15] = 0.995


def block(day: str, peak: float, actual_peak=None, actual_hours=0, da_peak=None):
    """One of the feed's day blocks: 24 hour-ending rows shaped by SHAPE."""
    rows = []
    for i, s in enumerate(SHAPE):
        r = {"hourEnding": i + 1, "dstFlag": "N", "currentLoadForecast": peak * s,
             "dayAheadForecast": (da_peak or peak) * s,
             "timestamp": f"{day} {i + 1:02d}:00:00-0500"}
        if i < actual_hours:
            r["systemLoad"] = (actual_peak or peak) * s
        rows.append(r)
    return {"dayDate": f"{day} 00:00:00-0500", "data": rows}


def feed(updated="2026-10-26 05:15:01-0500", prev_peak=58288.0, cur_peak=61789.0,
         cur_hours=5, next_peak=63000.0):
    return {"lastUpdated": updated,
            "previousDay": block("2026-10-25", prev_peak, prev_peak, 24),
            "currentDay": block("2026-10-26", cur_peak, cur_peak * 0.99, cur_hours, 62097.0),
            "nextDay": block("2026-10-27", next_peak)}


def kalshi_markets(strikes=range(58000, 67001, 500), rules=RULES):
    return {"markets": [{"event_ticker": EV, "ticker": f"{EV}-T{k}", "strike_type": "greater",
                         "floor_strike": k, "rules_primary": rules.format(k=k),
                         "close_time": "2026-10-27T04:55:00Z",
                         "yes_bid_dollars": "0.40", "yes_ask_dollars": "0.45"}
                        for k in strikes]}


def no_network(*a, **k):
    raise AssertionError("network call in a test")


class TestTimeAndParsing(unittest.TestCase):

    def test_central_time(self):
        utc = timezone.utc
        self.assertEqual(ef.central_offset_hours(datetime(2026, 10, 26, 15, tzinfo=utc)), -5)
        self.assertEqual(ef.central_offset_hours(datetime(2026, 12, 1, 15, tzinfo=utc)), -6)
        # fall back at 07:00Z on Sunday Nov 1 2026, spring forward at 08:00Z Mar 14 2027
        self.assertEqual(ef.central_offset_hours(datetime(2026, 11, 1, 6, 59, tzinfo=utc)), -5)
        self.assertEqual(ef.central_offset_hours(datetime(2026, 11, 1, 7, 0, tzinfo=utc)), -6)
        self.assertEqual(ef.central_offset_hours(datetime(2027, 3, 14, 7, 59, tzinfo=utc)), -6)
        self.assertEqual(ef.central_offset_hours(datetime(2027, 3, 14, 8, 0, tzinfo=utc)), -5)
        self.assertEqual(ef.local_to_utc(date(2026, 10, 26), 10),
                         datetime(2026, 10, 26, 15, tzinfo=utc))
        self.assertEqual(ef.local_to_utc(date(2026, 12, 1), 10),
                         datetime(2026, 12, 1, 16, tzinfo=utc))
        self.assertEqual(ef.local_to_utc(date(2026, 10, 26), -0.25),     # 23:45 the day before
                         datetime(2026, 10, 26, 4, 45, tzinfo=utc))
        self.assertEqual(ef.central_date(datetime(2026, 10, 26, 4, 30, tzinfo=utc)),
                         date(2026, 10, 25))

    def test_rules_and_ticker_day(self):
        self.assertEqual(ef.rules_day(RULES.format(k=65000)), "2026-10-26")
        self.assertIsNone(ef.rules_day("If the 5-minute load on October 26, 2026 is above"))
        self.assertEqual(ef.ticker_day(EV), "2026-10-26")
        self.assertIsNone(ef.ticker_day("KXTXERCOTPEAKD"))

    def test_parse_feed(self):
        js = feed()
        got = ef.parse_feed(js)
        self.assertEqual(got["updated"], "2026-10-26T10:15:01Z")
        self.assertEqual(sorted(got["days"]), ["2026-10-25", "2026-10-26", "2026-10-27"])
        row = got["days"]["2026-10-26"][0]
        self.assertEqual(row[:2], [1, "N"])
        self.assertAlmostEqual(row[2], 61789.0 * SHAPE[0])
        self.assertIsNotNone(row[4])                      # HE1 has run
        self.assertIsNone(got["days"]["2026-10-26"][10][4])
        # a short block and a broken value drop that day only
        js["nextDay"]["data"] = js["nextDay"]["data"][:20]
        js["previousDay"]["data"][3]["currentLoadForecast"] = 5.0
        self.assertEqual(sorted(ef.parse_feed(js)["days"]), ["2026-10-26"])
        self.assertEqual(ef.parse_feed("nope"), {"updated": None, "days": {}})

    def test_day_stats(self):
        s = ef.day_stats(ef.parse_feed(feed())["days"]["2026-10-26"])
        self.assertEqual((round(s["f_peak"]), s["peak_he"], s["act_hours"]), (61789, 17, 5))
        self.assertEqual((round(s["f_rest"]), s["rest_he"]), (61789, 17))
        self.assertAlmostEqual(s["da_peak"], 62097.0)
        self.assertAlmostEqual(s["act_max"], 61789.0 * 0.99 * SHAPE[0])
        done = ef.day_stats(ef.parse_feed(feed())["days"]["2026-10-25"])
        self.assertIsNone(done["f_rest"])


class TestModel(unittest.TestCase):

    def test_t4_cdf(self):
        self.assertEqual(ef.t4_cdf(0.0), 0.5)
        self.assertAlmostEqual(ef.t4_cdf(1.0), 0.81304952, places=7)
        self.assertAlmostEqual(ef.t4_cdf(-2.0), 0.05805826, places=7)
        self.assertAlmostEqual(ef.t4_cdf(3.0) + ef.t4_cdf(-3.0), 1.0)
        self.assertGreater(ef.t4_cdf(50.0), 0.9999)

    def test_probabilities(self):
        mu, sc = math.log(60000), 0.02
        self.assertAlmostEqual(ef.p_above(mu, sc, 60000), 0.5)
        ps = [ef.p_above(mu, sc, k) for k in range(56000, 64001, 500)]
        self.assertTrue(all(a > b for a, b in zip(ps, ps[1:])))
        self.assertEqual(ef.market_p({"type": "greater", "floor": 30000.0}, mu, sc), ef.P_MAX)
        self.assertEqual(ef.market_p({"type": "greater", "floor": 90000.0}, mu, sc), ef.P_MIN)
        lo = ef.market_p({"type": "less", "cap": 59000.0}, mu, sc)
        self.assertAlmostEqual(lo, 1 - ef.p_above(mu, sc, 59000), places=9)
        mid = ef.market_p({"type": "between", "floor": 59000.0, "cap": 61000.0}, mu, sc)
        self.assertAlmostEqual(mid, ef.p_above(mu, sc, 59000) - ef.p_above(mu, sc, 61000))
        self.assertIsNone(ef.market_p({"type": "functional", "floor": 1.0}, mu, sc))
        self.assertIsNone(ef.market_p({"type": "greater", "floor": None}, mu, sc))

    def test_hours_already_run_floor_the_peak(self):
        mu, sc = math.log(60000), 0.02
        # an hour has printed 58,200: "above 58,000" is settled, "above 58,500" is not
        self.assertEqual(ef.market_p({"type": "greater", "floor": 58000.0}, mu, sc, 58200.0),
                         ef.P_MAX)
        self.assertAlmostEqual(ef.market_p({"type": "greater", "floor": 58500.0}, mu, sc,
                                           58200.0), ef.p_above(mu, sc, 58500), places=9)
        self.assertEqual(ef.market_p({"type": "less", "cap": 58000.0}, mu, sc, 58200.0), ef.P_MIN)
        self.assertEqual(ef.market_p({"type": "between", "floor": 57000.0, "cap": 58000.0},
                                     mu, sc, 58200.0), ef.P_MIN)
        self.assertAlmostEqual(ef.market_p({"type": "between", "floor": 58000.0, "cap": 61000.0},
                                           mu, sc, 58200.0), 1 - ef.p_above(mu, sc, 61000))

    def test_scale_grows_with_the_swing(self):
        self.assertAlmostEqual(ef.scale_for(0.0), ef.S0 * ef.LEAD_MULT)
        self.assertAlmostEqual(ef.scale_for(-0.05), ef.S0 * (1 + ef.K_SWING * 0.05) * ef.LEAD_MULT)
        self.assertEqual(ef.scale_for(None), ef.scale_for(ef.SWING_UNKNOWN))
        self.assertEqual(ef.scale_for(0.9), ef.scale_for(ef.SWING_CAP))

    def test_quote_until(self):
        utc = timezone.utc
        d = date(2026, 10, 26)
        self.assertEqual(ef.quote_until(d, 17), datetime(2026, 10, 26, 15, tzinfo=utc))   # 10:00
        self.assertEqual(ef.quote_until(d, 16), datetime(2026, 10, 26, 14, tzinfo=utc))   # 09:00
        # a winter morning peak (HE8, starts 07:00 CST) stops 6 h before it
        self.assertEqual(ef.quote_until(date(2027, 1, 15), 8),
                         datetime(2027, 1, 15, 7, tzinfo=utc))

    def test_event_fair(self):
        days = ef.parse_feed(feed())["days"]
        utc = timezone.utc
        e = ef.event_fair("2026-10-26", days, datetime(2026, 10, 26, 10, tzinfo=utc))
        self.assertNotIn("why", e)
        self.assertEqual((e["forecast_peak_mw"], e["peak_he"], e["prev_peak_mw"]),
                         (61789, 17, 58288))
        self.assertAlmostEqual(e["swing"], round(math.log(61789 / 58288), 4))
        self.assertAlmostEqual(e["mu"], math.log(61789) + ef.BIAS)
        self.assertAlmostEqual(e["sc"], ef.scale_for(math.log(61789 / 58288)))
        self.assertEqual(e["quote_until"], "2026-10-26T15:00:00Z")
        late = ef.event_fair("2026-10-26", days, datetime(2026, 10, 26, 15, tzinfo=utc))
        self.assertIn("cutoff", late["why"])
        self.assertNotIn("mu", late)
        t = datetime(2026, 10, 26, 10, tzinfo=utc)
        self.assertIn("no ERCOT forecast", ef.event_fair("2026-10-29", days, t)["why"])
        # the day's actual load already at the forecast peak (a cold morning)
        hot = ef.parse_feed(feed(cur_hours=9))["days"]
        for r in hot["2026-10-26"][:9]:
            r[4] = 61000.0
        self.assertIn("realized", ef.event_fair("2026-10-26", hot,
                                                datetime(2026, 10, 26, 10, tzinfo=utc))["why"])
        # no previous day in the feed: priced as a swing day
        only = {"2026-10-26": days["2026-10-26"]}
        e2 = ef.event_fair("2026-10-26", only, datetime(2026, 10, 26, 10, tzinfo=utc))
        self.assertIsNone(e2["swing"])
        self.assertAlmostEqual(e2["sc"], ef.scale_for(None))
        # a day already over
        self.assertIn("every hour", ef.event_fair("2026-10-25", days,
                                                  datetime(2026, 10, 26, 10, tzinfo=utc))["why"])

    def test_a_winter_morning_hump_moves_the_stop(self):
        rows = ef.parse_feed(feed())["days"]["2026-10-26"]
        self.assertEqual(ef.gap_he(rows), 17)                  # an autumn day: the peak hour
        for r in rows:
            if r[0] == 8:
                r[2] = 0.98 * 61789.0                          # HE8 within 3% of HE17
        self.assertEqual(ef.gap_he(rows), 8)
        e = ef.event_fair("2026-10-26", {"2026-10-26": rows},
                          datetime(2026, 10, 26, 6, 30, tzinfo=timezone.utc))   # 01:30 CDT
        self.assertEqual(e["quote_until"], "2026-10-26T06:00:00Z")           # 01:00 CDT
        self.assertIn("cutoff", e["why"])

    def test_a_front_day_peaking_overnight_gets_no_fair(self):
        # 10/16/2026: the cold front's day peaked in HE01 (54,532 MW). A forecast
        # whose top hour is overnight leaves no window at all.
        js = feed()
        for i, r in enumerate(js["currentDay"]["data"]):
            r["currentLoadForecast"] = 56000.0 - 150.0 * i
            r.pop("systemLoad", None)
        days = ef.parse_feed(js)["days"]
        e = ef.event_fair("2026-10-26", days, datetime(2026, 10, 26, 4, 58,
                                                       tzinfo=timezone.utc))   # 23:58 CDT
        self.assertEqual(e["peak_he"], 1)
        self.assertIn("cutoff", e["why"])


class TestFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "ercot_fair.json")
        p = mock.patch.object(ef.requests, "get", side_effect=no_network)
        self.net = p.start()
        self.addCleanup(p.stop)
        self.public_fallbacks = 0          # Kalshi's public URL tried (and refused here)
        self.addCleanup(lambda: self.assertEqual(self.net.call_count, self.public_fallbacks))
        self.feed = feed()
        self.feed_calls = 0
        self.kalshi_calls = 0
        self.markets = kalshi_markets()

    def get_text(self, url, headers):
        self.assertEqual(url, ef.SWD_URL)
        self.feed_calls += 1
        if isinstance(self.feed, Exception):
            raise self.feed
        return json.dumps(self.feed)

    def get_json(self, path, params):
        self.assertEqual((path, params["series_ticker"], params["status"]),
                         ("/markets", "KXTXERCOTPEAKD", "open"))
        self.kalshi_calls += 1
        return self.markets

    def write(self, now):
        return ef.write_fair_file(self.path, now=now, get_json=self.get_json,
                                  get_text=self.get_text)

    def read(self, path=None) -> dict:
        with open(path or self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_writes_the_loader_shape(self):
        now = datetime(2026, 10, 26, 10, 20, tzinfo=timezone.utc)     # 05:20 CDT
        self.assertEqual(self.write(now), (19, 0))
        js = self.read()
        m = js["markets"][f"{EV}-T61500"]
        self.assertEqual((m["event"], m["fetched_at"], m["strike"], m["day"]),
                         (EV, "2026-10-26T10:20:00Z", 61500.0, "2026-10-26"))
        self.assertEqual(m["forecast_peak_mw"], 61789)
        e = js["event_fair"][EV]
        mu, sc = math.log(61789) + ef.BIAS, ef.scale_for(math.log(61789 / 58288))
        self.assertAlmostEqual(m["p"], round(ef.p_above(mu, sc, 61500), 4))
        self.assertEqual(m["sd_mw"], round(math.sqrt(2) * sc * 61789))
        self.assertEqual(e["quote_until"], "2026-10-26T15:00:00Z")
        ps = [js["markets"][f"{EV}-T{k}"]["p"] for k in range(58000, 67001, 500)]
        self.assertTrue(all(a >= b for a, b in zip(ps, ps[1:])))
        self.assertEqual(js["feed_at"], now.timestamp())
        # the history: this hour's forecast for today and tomorrow, the day-ahead
        # peaks, yesterday's actual peak
        h = self.read(ef.history_path_for(self.path))
        self.assertEqual(h["2026-10-26"]["fc"], {"2026-10-26T10:00Z": 61789.0})
        self.assertEqual(h["2026-10-26"]["da"], 62097.0)
        self.assertEqual(h["2026-10-25"]["y"], 58288.0)
        self.assertEqual(h["2026-10-27"]["fc"], {})          # before 18:00 the day before

    def test_a_failed_read_keeps_the_old_fetched_at_and_then_ages_out(self):
        t0 = datetime(2026, 10, 26, 10, 20, tzinfo=timezone.utc)
        self.write(t0)
        self.feed = OSError("down")
        ok, miss = self.write(t0 + timedelta(minutes=10))
        self.assertEqual((ok, miss), (19, 0))
        js = self.read()
        self.assertEqual(js["markets"][f"{EV}-T61500"]["fetched_at"], "2026-10-26T10:20:00Z")
        self.assertEqual(js["feed_at"], t0.timestamp())
        # the cached copy's own lastUpdated (05:15 CDT) is past FEED_MAX_AGE_MIN: nothing
        ok, miss = self.write(datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc))
        self.assertEqual((ok, miss), (0, 1))
        js = self.read()
        self.assertEqual(js["markets"], {})
        self.assertEqual(js["event_fair"][EV]["why"], "no fresh ERCOT feed")
        self.assertEqual(js["feed_at"], t0.timestamp())      # the old copy is kept, aged

    def test_a_stale_feed_is_a_failed_read(self):
        self.feed = feed(updated="2026-10-26 02:00:00-0500")         # 07:00Z
        ok, miss = self.write(datetime(2026, 10, 26, 10, 20, tzinfo=timezone.utc))
        self.assertEqual((ok, miss), (0, 1))
        self.assertFalse(os.path.exists(ef.history_path_for(self.path)))
        # a lastUpdated hours in the future (a zone slip) is no better
        self.feed = feed(updated="2026-10-26 09:00:00-0500")         # 14:00Z
        self.assertEqual(self.write(datetime(2026, 10, 26, 10, 20, tzinfo=timezone.utc)), (0, 1))

    def test_past_the_cutoff_the_markets_are_absent(self):
        self.feed = feed(updated="2026-10-26 09:45:00-0500")
        ok, miss = self.write(datetime(2026, 10, 26, 14, 55, tzinfo=timezone.utc))
        self.assertEqual((ok, miss), (19, 0))                         # 09:55 CDT
        ok, miss = self.write(datetime(2026, 10, 26, 15, 5, tzinfo=timezone.utc))
        self.assertEqual((ok, miss), (0, 1))
        self.assertIn("cutoff", self.read()["event_fair"][EV]["why"])

    def test_the_new_day_lists_when_the_old_closes(self):
        old = kalshi_markets()
        for m in old["markets"]:
            m["event_ticker"] = "KXTXERCOTPEAKD-26OCT26"
            m["ticker"] = m["ticker"].replace("26OCT27", "26OCT26")
            m["rules_primary"] = m["rules_primary"].replace("October 26", "October 25")
            m["close_time"] = "2026-10-26T04:55:00Z"
        self.markets = old
        self.feed = feed(updated="2026-10-25 23:15:00-0500")
        self.write(datetime(2026, 10, 26, 4, 50, tzinfo=timezone.utc))   # 23:50 CDT
        self.assertEqual(self.kalshi_calls, 1)
        self.write(datetime(2026, 10, 26, 4, 53, tzinfo=timezone.utc))   # cached
        self.assertEqual(self.kalshi_calls, 1)
        self.markets = kalshi_markets()
        ok, _ = self.write(datetime(2026, 10, 26, 4, 56, tzinfo=timezone.utc))  # closed: re-read
        self.assertEqual(self.kalshi_calls, 2)
        self.assertEqual(ok, 19)
        self.assertEqual(self.read()["events"][EV]["day"], "2026-10-26")

    def test_rules_that_do_not_name_the_metric_get_no_fair(self):
        self.markets = kalshi_markets(rules="If the 5-minute peak on October 26, 2026 is "
                                            "above {k}")
        ok, miss = self.write(datetime(2026, 10, 26, 10, 20, tzinfo=timezone.utc))
        self.assertEqual((ok, miss), (0, 1))
        self.assertIn("rules", self.read()["event_fair"][EV]["why"])

    def test_kalshi_down_reuses_the_cache(self):
        now = datetime(2026, 10, 26, 10, 20, tzinfo=timezone.utc)
        self.write(now)

        def broken(path, params):
            raise OSError("429")
        self.public_fallbacks = 1          # the signed read failed: the public URL is next
        ok, _ = ef.write_fair_file(self.path, now=now + timedelta(minutes=20), get_json=broken,
                                   get_text=self.get_text)
        self.assertEqual(ok, 19)

    def test_the_dry_run_cli_prints_every_open_market(self):
        import contextlib
        import io
        out = os.path.join(self.tmp, "dry.json")
        buf = io.StringIO()
        with mock.patch.object(ef, "_signed_or_public", return_value=self.markets), \
                mock.patch.object(ef, "_http_text", side_effect=self.get_text), \
                contextlib.redirect_stdout(buf):
            self.assertEqual(ef.main(["--out", out]), 0)
        text = buf.getvalue()
        self.assertTrue(os.path.exists(out))
        self.assertEqual(text.count(f"{EV}-T"), 19)              # each open market, with its book
        self.assertIn("book 40 x 45", text)
        self.assertIn(EV, self.read(out)["event_fair"])
        self.assertTrue(ef.DRY_OUT_DEFAULT.startswith(tempfile.gettempdir()))

    def test_history_keeps_one_read_per_hour_and_prunes(self):
        hist = {"2025-01-01": {"fc": {}, "y": 1.0}}
        f = ef.parse_feed(feed())
        now = datetime(2026, 10, 26, 10, 20, tzinfo=timezone.utc)
        self.assertTrue(ef.record_history(hist, f, now))
        self.assertNotIn("2025-01-01", hist)
        self.assertFalse(ef.record_history(hist, f, now + timedelta(minutes=5)))
        self.assertTrue(ef.record_history(hist, f, now + timedelta(hours=1)))
        self.assertEqual(len(hist["2026-10-26"]["fc"]), 2)


if __name__ == "__main__":
    unittest.main()
