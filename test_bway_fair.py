"""Tests for bway_fair.py (Broadway weekly attendance; Jack 2026-10-25: "Can
you build a fair value for this?" -> "Yes enroll it"). No network: the
League page, its calendar and Kalshi are handed in."""
import json
import math
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest import mock

import bway_fair as bf

EV = "KXBWAYATTENDANCEW-26NOV01"


def page(week: str, rows) -> str:
    """The League's grosses table for one week: rows of (show, sid, theatre,
    previews, performances, attend, cap), the commented-out cell included."""
    trs = "".join(
        f"<tr><td>{week}</td><td><a href=\"/research/grosses-broadway-nyc/{sid}/9/\" "
        f"class=\"show_details more\">{show}</a></td><td>Musical</td><td>{theatre}</td>"
        f"<td>{prev}</td><td>{perf}</td><td>$1,000,000</td><td class=\"inactive\">$1</td>"
        f"<!--<td>0%</td>--><td>{attend:,}</td><td class=\"inactive\">1</td><td>{cap}%</td></tr>"
        for show, sid, theatre, prev, perf, attend, cap in rows)
    return (f'<html><table class="display shows" width="100%"><thead><tr><th>Week End</th>'
            f'</tr></thead><tbody>{trs}</tbody></table></html>')


def rows(n=10, attend=8000, perfs=8, scale=1.0):
    return [{"show": f"SHOW {i}", "sid": str(100 + i), "theatre": f"Theatre {i}", "n": perfs,
             "attend": int(attend * scale), "cap": 95.0} for i in range(n)]


class TestPages(unittest.TestCase):

    def test_grosses_rows_skip_the_commented_cell(self):
        got = bf.parse_rows(page("10/18/2026", [("WANTED", "547555", "James Earl Jones", 5, 0,
                                                 5222, 100),
                                                ("& JULIET", "534962", "Stephen Sondheim", 0, 8,
                                                 7894, 96)]))
        self.assertEqual(list(got), ["2026-10-18"])
        w, j = got["2026-10-18"]
        self.assertEqual(w, {"show": "WANTED", "sid": "547555", "theatre": "James Earl Jones",
                             "n": 5, "attend": 5222, "cap": 100.0})
        self.assertEqual(j["show"], "& JULIET")
        self.assertEqual(bf.parse_rows("<html>no table</html>"), {})

    def test_calendar(self):
        cal = bf.parse_calendar([
            {"title": "Downstate", "theatre": "Booth Theatre", "preview": "October 22, 2026",
             "closing": "January 10, 2027", "grosses": None},
            {"title": "A Few Good Men", "theatre": "Vivian Beaumont Theater",
             "preview": "October 8, 2026", "closing": "January 10, 2027",
             "grosses": "/research/grosses-broadway-nyc/547369/"},
            {"title": "Now You See Me Live", "theatre": "Al Hirschfeld Theatre",
             "preview": "", "closing": "January 3, 2027"}])
        self.assertEqual(cal[0], {"title": "Downstate", "theatre": "Booth",
                                  "preview": "2026-10-22", "closing": "2027-01-10", "sid": ""})
        self.assertEqual((cal[1]["theatre"], cal[1]["sid"]), ("Vivian Beaumont", "547369"))
        self.assertIsNone(cal[2]["preview"])
        self.assertEqual(bf.parse_calendar({"not": "a list"}), [])

    def test_event_week_end(self):
        self.assertEqual(bf.event_week_end(EV), "2026-11-01")
        self.assertEqual(bf.event_week_end("KXBWAYATTENDANCEW-26OCT25"), "2026-10-25")
        self.assertIsNone(bf.event_week_end("KXBWAYATTENDANCEW"))


class TestModel(unittest.TestCase):

    def test_performance_counts(self):
        sun = date(2026, 10, 25)
        self.assertEqual(bf.first_week_perfs(date(2026, 10, 22), sun), 5)    # a Thursday
        self.assertEqual(bf.first_week_perfs(date(2026, 10, 24), sun), 2)    # a Saturday
        self.assertEqual(bf.first_week_perfs(date(2026, 10, 20), sun), 8)    # a Tuesday
        self.assertEqual(bf.closing_perfs(sun, sun, 8), 8)                    # closes Sunday
        self.assertEqual(bf.closing_perfs(date(2026, 10, 21), sun, 8), 3)     # ... Wednesday
        self.assertEqual(bf.closing_perfs(sun, sun, 7), 7)                    # its usual week
        weeks = {"2026-10-04": [{"show": "A", "n": 5}], "2026-10-11": [{"show": "A", "n": 7}],
                 "2026-10-18": [{"show": "A", "n": 7}, {"show": "B", "n": 5}]}
        self.assertEqual(bf.expected_perfs(weeks, "A", "2026-10-18"), 7)     # a solo play's 7
        self.assertEqual(bf.expected_perfs(weeks, "B", "2026-10-18"), 8)     # only a first week

    def test_season_drift_by_calendar_date_from_earlier_seasons(self):
        drift = {"2025-11-02": -0.04, "2024-11-03": -0.01, "2023-10-29": -0.03,
                 "2023-10-22": 0.5, "2026-10-25": 0.9}      # too far, and this season
        self.assertAlmostEqual(bf.season_drift(drift, date(2026, 11, 1)), -0.08 / 3)
        self.assertEqual(bf.season_drift({}, date(2026, 11, 1)), 0.0)

    def test_sigma_and_holiday_weeks(self):
        self.assertEqual(bf.sigma_for(1, date(2026, 10, 25)), 0.037)
        self.assertEqual(bf.sigma_for(2, date(2026, 11, 1)), 0.040)
        self.assertAlmostEqual(bf.sigma_for(3, date(2026, 11, 8)), 0.045)
        self.assertTrue(bf.holiday_week(date(2026, 11, 29)))     # Thanksgiving Nov 26
        self.assertTrue(bf.holiday_week(date(2027, 1, 3)))
        self.assertFalse(bf.holiday_week(date(2026, 11, 22)))
        self.assertAlmostEqual(bf.sigma_for(1, date(2026, 11, 29)), 0.037 * 1.5)

    def test_p_above(self):
        self.assertAlmostEqual(bf.p_above(260000, 0.04, 260000), 0.5)
        self.assertGreater(bf.p_above(260000, 0.04, 250000), 0.8)
        self.assertLess(bf.p_above(260000, 0.04, 280000), 0.05)
        self.assertEqual(bf.p_above(0, 0.04, 1), 0.0)

    def test_forecast_continuing_new_and_closing(self):
        base = rows(n=4)                                    # 4 x 8,000
        base.append({"show": "A FEW GOOD MEN 2026", "sid": "547369", "theatre": "Beaumont",
                     "n": 7, "attend": 7000, "cap": 100.0})
        base.append({"show": "CLOSER", "sid": "9", "theatre": "Old", "n": 8, "attend": 8000,
                     "cap": 90.0})
        weeks = {"2026-10-18": base}
        cal = [
            # a revival already in the grosses: matched by its id, not counted again
            {"title": "A Few Good Men", "theatre": "Vivian Beaumont", "preview": "2026-10-08",
             "closing": "2027-01-10", "sid": "547369"},
            # new: its first preview on a Thursday of the target week
            {"title": "Downstate", "theatre": "Booth", "preview": "2026-10-22",
             "closing": "2027-01-10", "sid": ""},
            # later than the target week
            {"title": "Much Ado", "theatre": "Winter Garden", "preview": "2026-10-31",
             "closing": None, "sid": ""}]
        seats = {"Booth": 772}
        total, parts = bf.forecast(weeks, {}, seats, cal, "2026-10-25", "2026-10-18",
                                   closings={"CLOSER": "2026-10-21"})
        self.assertEqual(parts["SHOW 0"], 8000.0)
        self.assertEqual(parts["A FEW GOOD MEN 2026"], 7000.0)        # its usual 7
        self.assertAlmostEqual(parts["CLOSER"], 3 * 1000.0)           # out after Wednesday
        self.assertAlmostEqual(parts["DOWNSTATE"], 5 * 772 * bf.FIRST_FILL)
        self.assertNotIn("A FEW GOOD MEN", parts)
        self.assertNotIn("MUCH ADO", parts)
        self.assertAlmostEqual(total, sum(parts.values()))
        # two weeks out: the closer gone, Downstate a full week at the later fill,
        # Much Ado's first two performances; the drift of both weeks applied
        drift = {"2025-10-26": math.log(0.98), "2025-11-02": math.log(0.95)}
        total2, parts2 = bf.forecast(weeks, drift, seats, cal, "2026-11-01", "2026-10-18",
                                     closings={"CLOSER": "2026-10-21"})
        self.assertNotIn("CLOSER", parts2)
        self.assertAlmostEqual(parts2["SHOW 0"], 8000 * 0.98 * 0.95)
        self.assertAlmostEqual(parts2["DOWNSTATE"], 8 * 772 * bf.LATER_FILL)
        self.assertAlmostEqual(parts2["MUCH ADO"], 2 * bf.DEFAULT_SEATS * bf.FIRST_FILL)
        # a calendar closing counts like a hand one; theatre names match inside
        # each other ("Samuel J. Friedman" is the grosses' "Friedman")
        self.assertEqual(bf.seats_for({"Friedman": 650}, "Samuel J. Friedman"), 650.0)
        cal2 = [{"title": "Show 1", "theatre": "", "preview": None, "closing": "2026-10-18",
                 "sid": ""}]
        _t, p3 = bf.forecast({"2026-10-18": rows(n=2)}, {}, {}, cal2, "2026-10-25",
                             "2026-10-18", closings={})
        self.assertEqual(set(p3), {"SHOW 0"})


class TestFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "bway_fair.json")
        seed = {"weeks": {"2026-10-11": rows(), "2026-10-18": rows()},
                "drift": {"2025-11-02": math.log(0.97)}, "seats": {"Booth": 772}}
        self.seed = os.path.join(self.tmp, "seed.json")
        with open(self.seed, "w", encoding="utf-8") as f:
            json.dump(seed, f)
        p = mock.patch.object(bf, "SEED_FILE", self.seed)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        self.league = page("10/18/2026", [(f"SHOW {i}", str(100 + i), f"Theatre {i}", 0, 8,
                                           8000, 95) for i in range(10)])
        self.calendar = [{"title": "Downstate", "theatre": "Booth Theatre",
                          "preview": "October 22, 2026", "closing": "January 10, 2027"}]

    def get_text(self, url, headers):
        self.calls.append(url)
        if url == bf.LEAGUE_URL:
            return self.league
        if url == bf.CALENDAR_URL:
            self.assertEqual(headers.get("X-Requested-With"), "XMLHttpRequest")
            return json.dumps(self.calendar)
        raise AssertionError(url)

    def get_json(self, path, params):
        self.assertEqual((path, params["series_ticker"]), ("/markets", "KXBWAYATTENDANCEW"))
        return {"markets": [
            {"event_ticker": EV, "ticker": f"{EV}-A{k}", "floor_strike": k,
             "strike_type": "greater"} for k in (70000, 80000, 90000)]}

    def read(self) -> dict:
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def write(self, when):
        return bf.write_fair_file(self.path, now=when, get_json=self.get_json,
                                  get_text=self.get_text, closings={})

    def test_writes_the_loader_shape_and_keeps_its_memory(self):
        now = datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(self.write(now), (3, 0))
        js = self.read()
        m = js["markets"][f"{EV}-A80000"]
        self.assertEqual((m["event"], m["fetched_at"], m["strike"]),
                         (EV, "2026-10-26T00:00:00Z", 80000))
        e = js["event_fair"][EV]
        # 10 shows at 8,000 x the Halloween week's -3% + Downstate a full week
        self.assertEqual((e["base_week"], e["h"], e["sigma"]), ("2026-10-18", 2, 0.04))
        self.assertEqual(e["forecast"], round(80000 * 0.97 + 8 * 772 * bf.LATER_FILL))
        self.assertAlmostEqual(m["p"], bf.p_above(e["forecast"], 0.04, 80000), places=3)
        self.assertGreater(js["markets"][f"{EV}-A70000"]["p"], m["p"])
        # inside the TTLs nothing is read again
        self.calls.clear()
        self.write(datetime(2026, 10, 26, 0, 20, tzinfo=timezone.utc))
        self.assertEqual(self.calls, [])

    def test_a_newly_posted_week_joins_with_its_drift(self):
        self.write(datetime(2026, 10, 26, 0, 0, tzinfo=timezone.utc))
        self.league = page("10/25/2026", [(f"SHOW {i}", str(100 + i), f"Theatre {i}", 0, 8,
                                           8400, 95) for i in range(10)]
                           + [("NEWHOUSE", "7", "New Hall", 0, 8, 6000, 75)])
        self.write(datetime(2026, 10, 27, 0, 0, tzinfo=timezone.utc))
        js = self.read()
        self.assertIn("2026-10-25", js["weeks"])
        self.assertAlmostEqual(js["drift"]["2026-10-25"], round(math.log(8400 / 8000), 5))
        self.assertEqual(js["seats"]["New Hall"], 1000)
        e = js["event_fair"][EV]
        self.assertEqual((e["base_week"], e["h"]), ("2026-10-25", 1))

    def test_no_calendar_no_fair_and_a_published_week_is_not_priced(self):
        def no_calendar(url, headers):
            if url == bf.CALENDAR_URL:
                raise OSError("down")
            return self.get_text(url, headers)
        ok, miss = bf.write_fair_file(self.path, get_json=self.get_json, get_text=no_calendar,
                                      now=datetime(2026, 10, 26, tzinfo=timezone.utc))
        self.assertEqual((ok, miss), (0, 1))
        js = self.read()
        self.assertEqual(js["event_fair"][EV]["why"], "no calendar yet")
        # a week already posted (the market should be shut): no fair either
        ok, miss = bf.write_fair_file(
            self.path, get_json=self.get_json, get_text=self.get_text,
            now=datetime(2026, 10, 26, 7, tzinfo=timezone.utc),
            events={EV.replace("NOV01", "OCT18"): {"week_end": "2026-10-18",
                                                     "strikes": {"X-A1": 1.0}}})
        self.assertEqual((ok, miss), (0, 1))

    def test_old_weeks_are_dropped(self):
        seed = {"weeks": {f"2026-08-{d:02d}": rows() for d in (2, 9, 16, 23, 30)},
                "drift": {}, "seats": {}}
        seed["weeks"].update({f"2026-09-{d:02d}": rows() for d in (6, 13, 20, 27)})
        seed["weeks"].update({"2026-10-04": rows(), "2026-10-11": rows(),
                              "2026-10-18": rows()})
        seed["drift"] = {"2025-11-02": 0.0}
        seed["seats"] = {"Booth": 772}
        with open(self.seed, "w", encoding="utf-8") as f:
            json.dump(seed, f)
        self.write(datetime(2026, 10, 26, tzinfo=timezone.utc))
        js = self.read()
        self.assertEqual(len(js["weeks"]), bf.KEEP_WEEKS)
        self.assertEqual(min(js["weeks"]), "2026-08-16")

    def test_the_shipped_seed_reads(self):
        seed = bf._read_json(os.path.join(os.path.dirname(os.path.abspath(bf.__file__)),
                                          "bway_seed.json"))
        self.assertGreaterEqual(len(seed["drift"]), 200)
        self.assertIn("Booth", seed["seats"])
        last = max(seed["weeks"])
        self.assertEqual(sum(r["attend"] for r in seed["weeks"][last]), 259164)


if __name__ == "__main__":
    unittest.main()
