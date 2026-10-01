"""Tests for vercel_fair (the pre-D Vercel gate's fair values)."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import vercel_fair as vf


def history(lab_metric, days, start=date(2026, 7, 20), base=20.0, step=None):
    """A synthetic export table: one (lab, metric) series of `days` values."""
    vals = {}
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        vals[d] = base + (step(i) if step else 0.0)
    return {lab_metric: vals}


class TestTickersAndStrikes(unittest.TestCase):
    def test_measured_day_by_series(self):
        self.assertEqual(vf.measured_day("KXOPENVREQ-28SEP26"), date(2026, 9, 28))
        self.assertEqual(vf.measured_day("KXOPENVREQ-05OCT26"), date(2026, 10, 5))
        # open-weights: YYMMMDD is the day AFTER D
        self.assertEqual(vf.measured_day("KXOPENSOURCESHARE-26SEP29"), date(2026, 9, 28))
        self.assertIsNone(vf.measured_day("KXOPENVREQ-31SEP26"))
        self.assertIsNone(vf.measured_day("KXFOO-28SEP26"))
        self.assertIsNone(vf.measured_day("KXOPENVREQ"))

    def test_event_ticker_round_trip(self):
        for ev in ("KXOPENVREQ-28SEP26", "KXANTHVSPEND-05OCT26", "KXOPENSOURCESHARE-26SEP29"):
            s = ev.split("-")[0]
            self.assertEqual(vf.event_ticker_for(s, vf.measured_day(ev)), ev)

    def test_strike_and_rounding_threshold(self):
        self.assertEqual(vf.strike_of("KXANTHVREQ-28SEP26-T5P5"), 5.5)
        self.assertEqual(vf.strike_of("KXOPENVREQ-28SEP26-T21"), 21.0)
        self.assertEqual(vf.strike_of("KXOPENSOURCESHARE-26SEP29-T67.5"), 67.5)
        self.assertIsNone(vf.strike_of("KXOPENVREQ-28SEP26-B21"))
        # strictly above K after rounding to 1 dp (half up)
        self.assertAlmostEqual(vf.yes_threshold(5.5), 5.55)
        self.assertAlmostEqual(vf.yes_threshold(21.0), 21.05)
        self.assertAlmostEqual(vf.yes_threshold(72.25), 72.25)


class TestFairModel(unittest.TestCase):
    def test_entry_uses_h_day_changes_widened(self):
        # alternating +1 / -1 day to day -> every 2-day change is 0, every
        # 1-day change +-1
        tab = history(("openai", "requests"), 80, step=lambda i: 1.0 if i % 2 else 0.0)
        dates = sorted(tab[("openai", "requests")])
        x = vf.series_values(tab, "KXOPENVREQ", dates)
        last = date.fromisoformat(dates[-1])
        e2 = vf.fair_entry(x, last + timedelta(days=2), last, widen=1.5)
        self.assertEqual(e2["h"], 2)
        self.assertEqual(set(e2["errs"]), {0.0})
        e1 = vf.fair_entry(x, last + timedelta(days=1), last, widen=1.5)
        self.assertEqual(set(e1["errs"]), {-1.5, 1.5})             # +-1 widened
        self.assertIsNone(vf.fair_entry(x, last, last, widen=1.5))  # h < 1
        short = {d: v for d, v in list(x.items())[-10:]}
        self.assertIsNone(vf.fair_entry(short, last + timedelta(days=2), last, 1.5))

    def test_p_yes_counts_the_sample(self):
        entry = {"x_l": 20.0, "errs": [-1.0, -0.5, 0.0, 0.5, 1.0] * 4}
        self.assertAlmostEqual(vf.p_yes(entry, 20.0), 8 / 20)   # needs >= 20.05
        self.assertAlmostEqual(vf.p_yes(entry, 19.0), 16 / 20)  # 19.0 is not > 19
        self.assertAlmostEqual(vf.p_yes(entry, 18.9), 0.99)     # all -> clipped
        self.assertAlmostEqual(vf.p_yes(entry, 25.0), 0.01)

    def test_open_weights_sum(self):
        tab = {("deepseek", "tokens"): {"2026-09-01": 30.0},
               ("zai", "tokens"): {"2026-09-01": 20.0},
               ("openai", "tokens"): {"2026-09-01": 40.0}}
        x = vf.series_values(tab, "KXOPENSOURCESHARE", ["2026-09-01"])
        self.assertEqual(x["2026-09-01"], 50.0)

    def test_write_fair_file_keys_events_by_ticker(self):
        tab = {}
        for s, (lab, met) in vf.LAB_SERIES.items():
            tab.update(history((lab, met), 80, start=date(2026, 7, 10), base=20.0,
                               step=lambda i: (i % 5) * 0.3))
        for lab in vf.OPEN_WEIGHT_LABS[:2]:
            tab.update(history((lab, "tokens"), 80, start=date(2026, 7, 10), base=30.0))
        now = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
        path = os.path.join(tempfile.mkdtemp(), "vf.json")
        with mock.patch.object(vf, "RUN_ANCHOR", False):     # the 9/27 model
            n, miss, mode = vf.write_fair_file(path, now=now, tab=tab)
        self.assertEqual((n, miss, mode), (8 * vf.HORIZON_DAYS, 0, "complete"))
        data = json.load(open(path, encoding="utf-8"))
        e = data["entries"]["KXOPENVREQ-28SEP26"]
        self.assertEqual((e["d"], e["last"], e["h"]), ("2026-09-28", "2026-09-26", 2))
        self.assertEqual(e["anchor"], "complete")
        self.assertIn("KXOPENSOURCESHARE-26SEP29", data["entries"])
        self.assertEqual(data["model"]["lab_widen"], vf.LAB_WIDEN)

    def test_fetch_uses_a_fresh_distinct_to(self):
        r = mock.Mock()
        r.json.return_value = {"rows": [
            {"date": "2026-09-26", "name": "openai", "metric": "requests", "share_percent": 18.9},
            {"date": "2026-09-26", "name": "openai", "metric": "imageCount", "share_percent": 50}]}
        r.raise_for_status.return_value = None
        now = datetime(2026, 9, 27, 18, 7, tzinfo=timezone.utc)
        with mock.patch.object(vf.requests, "get", return_value=r) as g:
            tab = vf.fetch_labs(now)
        params = g.call_args.kwargs["params"]
        self.assertEqual((params["dataset"], params["modality"]), ("labs", "all"))
        self.assertGreater(params["to"], "2026-09-27")            # a distinct cache key
        self.assertEqual(tab, {("openai", "requests"): {"2026-09-26": 18.9}})
        r.json.return_value = {"rows": []}
        with mock.patch.object(vf.requests, "get", return_value=r):
            with self.assertRaises(RuntimeError):
                vf.fetch_labs(now)


NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)     # tau = 1080 min


def lab_table(today_block=True, today_val=30.0):
    """80 days of history for every series (ending 9/26) + an optional
    running block for 9/27 (117 rows, like the export's)."""
    tab = {}
    for _s, (lab, met) in vf.LAB_SERIES.items():
        tab.update(history((lab, met), 80, start=date(2026, 7, 9), base=20.0,
                           step=lambda i: (i % 5) * 0.3))
    for lab in vf.OPEN_WEIGHT_LABS[:2]:
        tab.update(history((lab, "tokens"), 80, start=date(2026, 7, 9), base=30.0))
    if today_block:
        for i in range(39):
            for met in ("spend", "requests", "tokens"):
                tab.setdefault((f"lab{i}", met), {})["2026-09-27"] = 0.1
        for _s, (lab, met) in vf.LAB_SERIES.items():
            tab[(lab, met)]["2026-09-27"] = today_val
        for lab in vf.OPEN_WEIGHT_LABS[:2]:
            tab[(lab, "tokens")]["2026-09-27"] = today_val
    return tab


def calib(days=3, delta=-2.0, minute=1080.0):
    """Calibration days ending 9/26 with final - running = delta + (0,
    +0.5, -0.5, ...) at the pass a minute before `minute`."""
    out = {}
    for j in range(days):
        d = (date(2026, 9, 26) - timedelta(days=j)).isoformat()
        off = (0.0, 0.5, -0.5, 1.0, -1.0)[j % 5]
        fin = {s: 20.0 for s in vf.ALL_SERIES}
        run = {s: 20.0 - delta - off for s in vf.ALL_SERIES}
        out[d] = {"final": fin, "run": [(minute - 1.0, run), (minute + 30.0, dict(fin))]}
    return out


class TestRunningAnchor(unittest.TestCase):
    """Jack 2026-10-01: "yes anchor the fair on D-1's running share"."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "vf.json")

    def _write(self, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("tab", lab_table())
        res = vf.write_fair_file(self.path, **kw)
        with open(self.path, encoding="utf-8") as f:
            return res, json.load(f)

    def test_run_entry_convolves_delta_and_k_day_changes(self):
        tab = history(("openai", "requests"), 80, step=lambda i: 1.0 if i % 2 else 0.0)
        dates = sorted(tab[("openai", "requests")])
        x = vf.series_values(tab, "KXOPENVREQ", dates)
        today = date.fromisoformat(dates[-1]) + timedelta(days=1)
        with mock.patch.object(vf, "RUN_WIDEN", 1.0):
            e1 = vf.run_entry(x, 25.0, [-1.0, 0.0, 1.0], today + timedelta(days=1),
                              today, widen=1.0)
            e2 = vf.run_entry(x, 25.0, [-1.0, 0.0, 1.0], today + timedelta(days=2),
                              today, widen=1.0)
        self.assertEqual((e1["x_l"], e1["h"], e1["calib_days"]), (25.0, 1, 3))
        # 1-day changes +-1, deltas -1/0/+1: every pairing
        self.assertEqual(set(e1["errs"]), {-2.0, -1.0, 0.0, 1.0, 2.0})
        self.assertEqual(e1["n"], len(e1["errs"]))
        self.assertEqual(set(e2["errs"]), {-1.0, 0.0, 1.0})    # 2-day changes 0
        # widening: delta about its mean, the changes about their median
        e3 = vf.run_entry(x, 25.0, [1.0, 2.0, 3.0], today + timedelta(days=2),
                          today, widen=1.0)
        self.assertEqual(set(e3["errs"]), {2.0 - 1.5, 2.0, 2.0 + 1.5})
        self.assertAlmostEqual(e3["delta_mean"], 2.0)
        self.assertIsNone(vf.run_entry(x, 25.0, [0.0, 1.0], today + timedelta(days=1),
                                       today, widen=1.0))     # < RUN_MIN_DAYS
        self.assertIsNone(vf.run_entry(x, 25.0, [0.0] * 3, today, today, 1.0))

    def test_writes_running_entries_past_the_start(self):
        (n, miss, mode), data = self._write(calib=calib(delta=-2.0))
        self.assertEqual((n, miss, mode), (8 * vf.HORIZON_DAYS, 0, "run"))
        e = data["entries"]["KXOPENVREQ-28SEP26"]               # D-1 = today
        self.assertEqual((e["anchor"], e["last"], e["h"]), ("run", "2026-09-27@run", 1))
        self.assertEqual(e["x_l"], 30.0)                        # today's running share
        self.assertAlmostEqual(e["delta_mean"], -2.0)
        self.assertEqual(e["tau_min"], 1080.0)
        self.assertEqual(data["entries"]["KXOPENVREQ-29SEP26"]["h"], 2)
        # the fair follows the running share, not yesterday's 20-ish close
        self.assertGreater(vf.p_yes(e, 26.0), 0.9)
        # open-weights: the summed running token share of the open-weight labs
        self.assertEqual(data["entries"]["KXOPENSOURCESHARE-26SEP29"]["x_l"], 60.0)
        self.assertEqual(data["anchor"]["mode"], "run")
        self.assertEqual(data["anchor"]["delta_n"]["KXOPENVREQ"], 3)

    def test_complete_anchor_before_the_start(self):
        early = datetime(2026, 9, 27, 0, 40, tzinfo=timezone.utc)
        (n, miss, mode), data = self._write(now=early, tab=lab_table(today_block=False),
                                            calib={})
        self.assertEqual((n, mode), (8 * vf.HORIZON_DAYS, "complete"))
        e = data["entries"]["KXOPENVREQ-28SEP26"]
        self.assertEqual((e["anchor"], e["last"], e["h"]), ("complete", "2026-09-26", 2))

    def test_fails_closed_without_a_running_block_or_calibration(self):
        (n, miss, mode), data = self._write(tab=lab_table(today_block=False),
                                            calib=calib())
        self.assertEqual((n, miss, mode), (0, 8, "fail:block"))
        self.assertEqual(data["entries"], {})
        (n, miss, mode), _ = self._write(calib=calib(days=2))
        self.assertEqual((n, miss, mode), (0, 8, "fail:calib"))
        (n, miss, mode), _ = self._write(calib={})
        self.assertEqual((n, mode), (0, "fail:calib"))
        # no calibration pass within RUN_TAU_TOL_MIN of now
        (n, miss, mode), _ = self._write(calib=calib(minute=1080.0 + 60))
        self.assertEqual((n, mode), (0, "fail:calib"))
        # one series short of days: only it goes without
        c = calib()
        for day in c.values():
            for _m, vals in day["run"]:
                vals.pop("KXDEEPVREQ", None)
        (n, miss, mode), data = self._write(calib=c)
        self.assertEqual((n, miss, mode), (7 * vf.HORIZON_DAYS, 1, "run"))
        self.assertNotIn("KXDEEPVREQ-28SEP26", data["entries"])

    def test_kill_switch_keeps_the_complete_anchor(self):
        with mock.patch.object(vf, "RUN_ANCHOR", False):
            (n, miss, mode), data = self._write(calib={})
        self.assertEqual((n, mode), (8 * vf.HORIZON_DAYS, "complete"))
        self.assertEqual(data["entries"]["KXOPENVREQ-28SEP26"]["x_l"],
                         round(lab_table()[("openai", "requests")]["2026-09-26"], 4))

    def test_block_usable(self):
        self.assertTrue(vf.block_usable(117, 117))
        self.assertTrue(vf.block_usable(100, 120))
        self.assertFalse(vf.block_usable(20, 117))      # the first minutes of a day
        self.assertFalse(vf.block_usable(90, 120))      # < 80% of yesterday's
        self.assertFalse(vf.block_usable(50, 50))       # < RUN_MIN_ROWS


def logger_pass(t, blocks):
    return json.dumps({"t": t, "cache": "MISS", "age": "0", "labs": blocks},
                      separators=(",", ":"))


def block(openai_req, rows=117):
    b = {f"lab{i}|spend": 0.1 for i in range(rows - 1)}
    b["openai|requests"] = openai_req
    return b


class TestLoggerCalibration(unittest.TestCase):
    """load_calibration reads the vercel-logger's per-day export files."""

    def setUp(self):
        import gzip
        self.dir = tempfile.mkdtemp()
        vf._parsed_files.clear()
        # 9/25 (gzipped): a degenerate 00:05 block, then 20.0 at 06:00, 24.0 at 12:00
        with gzip.open(os.path.join(self.dir, "export_2026-09-25.jsonl.gz"), "wt",
                       encoding="utf-8") as f:
            f.write(logger_pass("2026-09-25T00:05:00+00:00",
                                {"2026-09-24": block(1.0), "2026-09-25": block(99.0, 10)}) + "\n")
            f.write(logger_pass("2026-09-25T06:00:10+00:00",
                                {"2026-09-24": block(1.0), "2026-09-25": block(20.0)}) + "\n")
            f.write(logger_pass("2026-09-25T12:00:00+00:00",
                                {"2026-09-24": block(1.0), "2026-09-25": block(24.0)}) + "\n")
        # 9/26 (plain): 9/25's block settles 22.0 by 02:00, revised 23.0 after 14:00
        with open(os.path.join(self.dir, "export_2026-09-26.jsonl"), "w",
                  encoding="utf-8") as f:
            f.write(logger_pass("2026-09-26T00:30:00+00:00",
                                {"2026-09-25": block(21.5), "2026-09-26": block(30.0)}) + "\n")
            f.write(logger_pass("2026-09-26T03:00:00+00:00",
                                {"2026-09-25": block(22.0), "2026-09-26": block(31.0)}) + "\n")
            f.write("not json\n")
            f.write(logger_pass("2026-09-26T15:00:00+00:00",
                                {"2026-09-25": block(23.0), "2026-09-26": block(32.0)}) + "\n")

    def test_finals_and_running_paths(self):
        cal = vf.load_calibration(date(2026, 9, 26), self.dir)
        self.assertEqual(list(cal), ["2026-09-25"])          # 9/26 has no next-day file
        c = cal["2026-09-25"]
        self.assertEqual(c["final"]["KXOPENVREQ"], 22.0)     # 02:00-14:00Z read
        self.assertEqual([round(m) for m, _v in c["run"]], [360, 720])   # degenerate block out
        self.assertEqual(vf.delta_samples(cal, "KXOPENVREQ", 365.0), [2.0])
        self.assertEqual(vf.delta_samples(cal, "KXOPENVREQ", 715.0), [-2.0])
        self.assertEqual(vf.delta_samples(cal, "KXOPENVREQ", 540.0), [])   # no pass near
        # the next day's file is today's: only its final window is read
        cal2 = vf.load_calibration(date(2026, 9, 27), self.dir)
        self.assertEqual(sorted(cal2), ["2026-09-25"])
        self.assertEqual(vf.load_calibration(date(2026, 9, 27), self.dir + "-missing"), {})

    def test_parse_skips_lines_outside_the_final_window(self):
        p = os.path.join(self.dir, "export_2026-09-26.jsonl")
        out = vf.parse_logger_day(p, date(2026, 9, 26), want_run=False)
        self.assertEqual(out["run"], [])
        self.assertEqual(out["prev_final"]["KXOPENVREQ"], 22.0)
        full = vf.parse_logger_day(p, date(2026, 9, 26), want_run=True)
        self.assertEqual([v["KXOPENVREQ"] for _m, v in full["run"]], [30.0, 31.0, 32.0])


class TestFetchTable(unittest.TestCase):
    def test_history_once_a_day_then_a_short_fresh_read(self):
        vf._hist.update(day=None, at=0.0, tab=None)
        now = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
        long_tab = {("openai", "requests"): {"2026-09-26": 20.0, "2026-09-27": 25.0},
                    ("zai", "tokens"): {"2026-09-27": 3.0}}
        short_tab = {("openai", "requests"): {"2026-09-26": 20.0, "2026-09-27": 26.0}}
        calls = []

        def fake(now_, timeout=30, days_back=None):
            calls.append(days_back)
            return long_tab if days_back is None else short_tab
        with mock.patch.object(vf, "fetch_labs", side_effect=fake):
            t1 = vf.fetch_table(now)
            t2 = vf.fetch_table(now)
            vf._hist["at"] -= vf.HISTORY_REFRESH_SECS + 1
            vf.fetch_table(now)
            vf.fetch_table(now + timedelta(days=1))
        self.assertEqual(calls, [None, 1, None, None])
        self.assertEqual(t1[("openai", "requests")]["2026-09-27"], 25.0)
        self.assertEqual(t2[("openai", "requests")]["2026-09-27"], 26.0)
        # today's block is exactly the fresh read's: a lab missing from it is gone
        self.assertNotIn("2026-09-27", t2[("zai", "tokens")])
        vf._hist.update(day=None, at=0.0, tab=None)


if __name__ == "__main__":
    unittest.main()
