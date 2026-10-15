"""Tests for temp_shadow_model (index replica, WxTape, FairModel, fit/eval).

Sandboxed: TEMP_SHADOW_DATA points at a fresh temp dir BEFORE
temp_shadow_common is imported (it reads the env at import), so nothing here
reads or writes the live data root. Fixture rows are real study minutes
(Kalshi /live_data/weather/miami?detailed=true, Oct 7-13 2026) inlined.
Run: python -m unittest test_temp_shadow_model -v
"""
import os
import tempfile

os.environ["TEMP_SHADOW_DATA"] = tempfile.mkdtemp(prefix="tsmodel_test_")

import atexit  # noqa: E402
import contextlib  # noqa: E402
import csv  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import math  # noqa: E402
import random  # noqa: E402
import shutil  # noqa: E402
import unittest  # noqa: E402

import temp_shadow_common as C  # noqa: E402
import temp_shadow_model as M  # noqa: E402

assert C.DATA_ROOT == os.environ["TEMP_SHADOW_DATA"], "sandbox not applied"
atexit.register(shutil.rmtree, C.DATA_ROOT, True)

CAL_BODY = {"calibrations": [
    {"config_version": "miami-temperature-v1.0", "effective_at_ms": 1786665600000,
     "city_reference_c": -0.1,
     "stations": [{"station_id": "KMIA1M", "offset_c": 0, "weight": 0.2},
                  {"station_id": "KOPF1M", "offset_c": -0.5, "weight": 0.2},
                  {"station_id": "KFLL1M", "offset_c": 0, "weight": 0.2},
                  {"station_id": "KFXE1M", "offset_c": 0, "weight": 0.2},
                  {"station_id": "KPMP1M", "offset_c": 0, "weight": 0.2}]},
    {"config_version": "miami-temperature-v1.0-cal-20261005", "effective_at_ms": 1791158760000,
     "city_reference_c": 0.06015625,
     "stations": [{"station_id": "KMIA1M", "offset_c": 0.34375, "weight": 0.2},
                  {"station_id": "KOPF1M", "offset_c": -0.0390625, "weight": 0.2},
                  {"station_id": "KFLL1M", "offset_c": 0, "weight": 0.2},
                  {"station_id": "KFXE1M", "offset_c": -0.046875, "weight": 0.2},
                  {"station_id": "KPMP1M", "offset_c": 0.04296875, "weight": 0.2}]},
    {"config_version": "miami-temperature-v1.0-cal-20261012", "effective_at_ms": 1791763560000,
     "city_reference_c": 0.030078125,
     "stations": [{"station_id": "KMIA1M", "offset_c": 0.171875, "weight": 0.2},
                  {"station_id": "KOPF1M", "offset_c": -0.01953125, "weight": 0.2},
                  {"station_id": "KFLL1M", "offset_c": 0, "weight": 0.2},
                  {"station_id": "KFXE1M", "offset_c": -0.0234375, "weight": 0.2},
                  {"station_id": "KPMP1M", "offset_c": 0.021484375, "weight": 0.2}]},
]}


def _st(sid, f, code="ok", recv=None, source="hf_asos", **kw):
    d = {"station_id": sid, "code": code, "source": source}
    if f is not None:
        d["temp_f"] = f
    if recv is not None:
        d["received_at_ms"] = recv
    d.update(kw)
    return d


# Real study minutes (official v) covering every replica path.
ROW_NORMAL = {"t": 1791338400000, "status": "normal", "v": 81.68, "contributors": 5, "stations": [
    _st("KFLL1M", 82.4, recv=1791338600866), _st("KFXE1M", 80.6, recv=1791338539682),
    _st("KMIA1M", 82.4, recv=1791338540005), _st("KOPF1M", 82.4, recv=1791338540023),
    _st("KPMP1M", 80.6, recv=1791338540662)]}
ROW_METAR5 = {"t": 1791340380000, "status": "degraded", "v": 81.25, "contributors": 5, "stations": [
    _st("KFLL1M", 80.6, recv=1791340603934), _st("KFXE1M", 80.6, recv=1791340518573),
    _st("KMIA1M", 82.4, recv=1791340518229),
    _st("KOPF1M", 82.04, recv=1791338171872, source="metar", obs_time_ms=1791337980000,
        primary_code="missing"),
    _st("KPMP1M", 80.6, recv=1791340518890)]}
ROW_DEG4_CAL1005 = {"t": 1791594960000, "status": "degraded", "v": 84.29, "contributors": 4, "stations": [
    _st("KFLL1M", 84.91999999999999, recv=1791593774008, source="metar",
        obs_time_ms=1791593580000, primary_code="missing"),
    {"code": "missing", "station_id": "KFXE1M"},
    _st("KMIA1M", 84.2, recv=1791595107086),
    _st("KOPF1M", 84.02, recv=1791593773994, source="metar", obs_time_ms=1791593580000,
        primary_code="missing"),
    _st("KPMP1M", 84.2, recv=1791595107355)]}
ROW_DEG4_CAL1012 = {"t": 1791871800000, "status": "degraded", "v": 77.89, "contributors": 4, "stations": [
    {"code": "missing", "station_id": "KFLL1M"}, _st("KFXE1M", 77, recv=1791871942344),
    _st("KMIA1M", 80.6, recv=1791871943603), _st("KOPF1M", 78.8, recv=1791871943282),
    _st("KPMP1M", 75.2, recv=1791871944235)]}
ROW_LATE = {"t": 1791362940000, "status": "degraded", "v": 80.12, "contributors": 4, "stations": [
    _st("KFLL1M", 80.6, code="late", recv=1791363248556), _st("KFXE1M", 78.8, recv=1791363125944),
    _st("KMIA1M", 80.6, recv=1791363125944), _st("KOPF1M", 80.6, recv=1791363125944),
    _st("KPMP1M", 80.6, recv=1791363126094)]}
FIXTURES = [ROW_NORMAL, ROW_METAR5, ROW_DEG4_CAL1005, ROW_DEG4_CAL1012, ROW_LATE]


def cals():
    return M.Calibrations(M.parse_calibrations(CAL_BODY), source="fixture")


def f_of_c(c):
    return round(32 + 1.8 * c, 2)


def wx_row(t_ms, temps_c, rx, status="incomplete", v=None, codes=None):
    """A wx stream row; temps_c: {station: whole C}."""
    sts = []
    for sid, c in temps_c.items():
        sts.append({"station_id": sid, "temp_f": f_of_c(c),
                    "code": (codes or {}).get(sid, "pending" if v is None else "ok"),
                    "source": "hf_asos", "received_at_ms": int((rx - 1) * 1000)})
    return {"rx": rx, "city": "miami", "t": t_ms, "status": status, "v": v,
            "contributors": None, "stations": sts}


def minute_rows(t_ms, temps_c, fll_late=True):
    """Live-like rows for one minute: 4 stations at t+146 s, KFLL at t+200 s,
    official v at t+311 s."""
    ts = t_ms / 1000.0
    four = {k: v for k, v in temps_c.items() if k != "KFLL1M"}
    out = [wx_row(t_ms, four, ts + 146)]
    out.append(wx_row(t_ms, temps_c, ts + 200))
    s = sum(temps_c.values())
    out.append(wx_row(t_ms, temps_c, ts + 311, status="normal", v=C.index_from_sum(int(s))))
    return out


def flat_temps(c=27):
    return {s: c for s in M.STATIONS}


T0 = 1791633600000          # 2026-10-10 09:20Z (minute aligned)


class TestTime(unittest.TestCase):
    def test_est_hour_dst(self):
        # 09:00 EDT (13:00Z) is 08 EST; 09:00 EST in December is 09 EST
        s_oct = C.parse_event("KXTEMPMIAH-26OCT1409")[1]
        self.assertEqual(C.iso(s_oct), "2026-10-14T13:00:00Z")
        self.assertEqual(M.est_hour(s_oct), 8)
        self.assertEqual(M.bucket_of_hour(M.est_hour(s_oct)), "morning")
        s_dec = C.parse_event("KXTEMPMIAH-26DEC1409")[1]
        self.assertEqual(C.iso(s_dec), "2026-12-14T14:00:00Z")
        self.assertEqual(M.est_hour(s_dec), 9)
        # fall-back night: 01:30 EDT and 01:30 EST are different solar hours
        self.assertEqual(M.est_hour(C.parse_iso("2026-11-01T05:30:00Z")), 0)
        self.assertEqual(M.est_hour(C.parse_iso("2026-11-01T06:30:00Z")), 1)
        # month boundary in EST: 03:00Z on Nov 1 is still Oct 31 22:00 EST
        self.assertEqual(M.est_month(C.parse_iso("2026-11-01T03:00:00Z")), 10)

    def test_buckets_and_seasons(self):
        self.assertEqual([M.bucket_of_hour(h) for h in (0, 6, 7, 11, 12, 17, 18, 23)],
                         ["night", "night", "morning", "morning", "afternoon", "afternoon",
                          "evening", "evening"])
        self.assertEqual([M.season_of_month(m) for m in (6, 9, 10, 11, 12, 5)],
                         ["JJAS", "JJAS", "ON", "ON", "DJFMAM", "DJFMAM"])

    def test_drift_sign(self):
        fm = M.FairModel()
        morning = C.parse_iso("2026-10-14T14:00:00Z")   # 09 EST
        evening = C.parse_iso("2026-10-15T00:00:00Z")   # 19 EST
        self.assertGreater(fm.drift(morning, 60), 1.0)
        self.assertLess(fm.drift(evening, 60), -0.3)
        self.assertAlmostEqual(fm.drift(morning, 0), 0.0, places=9)
        # a full day of drift integrates to ~0 (closure)
        self.assertAlmostEqual(fm.drift(morning, 1440), 0.0, delta=0.15)


class TestReplica(unittest.TestCase):
    def test_fixture_rows_exact(self):
        cs = cals()
        for row in FIXTURES:
            rep = M.replica_for_row(row, cs, codes=frozenset(("ok",)))
            self.assertIsNotNone(rep, row["t"])
            self.assertAlmostEqual(rep, row["v"], places=6, msg=row["t"])

    def test_calibration_choice_matters(self):
        # the 10/12 4-station minute only matches under cal-20261012
        cs = cals()
        old = cs.by_version("miami-temperature-v1.0-cal-20261005")
        rep_old = M.replica_for_row(ROW_DEG4_CAL1012, cs, codes=frozenset(("ok",)), cal=old)
        self.assertNotAlmostEqual(rep_old, ROW_DEG4_CAL1012["v"], places=2)
        eff = 1791763560000
        self.assertEqual(cs.for_minute(eff - 60000).config_version,
                         "miami-temperature-v1.0-cal-20261005")
        self.assertEqual(cs.for_minute(eff).config_version, "miami-temperature-v1.0-cal-20261012")
        self.assertEqual(cs.for_minute(0).config_version, "miami-temperature-v1.0")

    def test_five_station_plain_mean(self):
        rng = random.Random(3)
        cs = cals()
        for _ in range(200):
            temps = {s: rng.randint(20, 33) for s in M.STATIONS}
            cal = cs.cals[rng.randrange(len(cs.cals))]
            rep = M.replica_index({s: f_of_c(c) for s, c in temps.items()}, cal.offsets,
                                  cal.b_city_c, cal.weights)
            self.assertAlmostEqual(rep, C.index_from_sum(sum(temps.values())), places=9)

    def test_needs_four_and_weight_share(self):
        offs = {s: 0.0 for s in M.STATIONS}
        three = {s: 80.6 for s in M.STATIONS[:3]}
        self.assertIsNone(M.replica_index(three, offs, 0.0))
        four = {s: 80.6 for s in M.STATIONS[:4]}
        self.assertEqual(M.replica_index(four, offs, 0.0), 80.6)
        w = {s: 0.2 for s in M.STATIONS}
        w["KPMP1M"] = 0.4               # 4 present carry 0.8/1.2 < 0.8
        self.assertIsNone(M.replica_index(four, offs, 0.0, weights=w))
        # unknown station ids are ignored
        self.assertIsNone(M.replica_index(dict(three, KXXX1M=80.6), offs, 0.0))

    def test_late_missing_excluded(self):
        st = M.row_stations(ROW_LATE)
        self.assertNotIn("KFLL1M", st)
        self.assertEqual(len(st), 4)
        self.assertNotIn("KFXE1M", M.row_stations(ROW_DEG4_CAL1005))

    def test_default_calibrations_when_no_file(self):
        cs = M.Calibrations.load()
        self.assertEqual(cs.source, "defaults")
        c = cs.for_minute(1791871800000)
        self.assertEqual(c.offsets["KOPF1M"], -0.5)
        self.assertEqual(c.b_city_c, -0.10)
        self.assertEqual(c.offsets["KMIA1M"], 0.0)

    def test_calibrations_file_loaded(self):
        p = os.path.join(C.DATA_ROOT, "backfill", "calibrations.json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            with open(p, "w") as f:
                json.dump(CAL_BODY, f)
            cs = M.Calibrations.load()
            self.assertEqual(cs.source, p)
            self.assertEqual(len(cs.cals), 3)
        finally:
            os.remove(p)

    def test_replica_check(self):
        res = M.replica_check(FIXTURES, cals())
        self.assertEqual(res["by_status"]["normal"], {"n": 1, "exact": 1, "none": 0, "rate": 1.0})
        self.assertEqual(res["by_status"]["degraded"]["rate"], 1.0)
        bad = json.loads(json.dumps(ROW_NORMAL))
        bad["v"] = 81.32
        res = M.replica_check([bad], cals())
        self.assertEqual(res["by_status"]["normal"]["exact"], 0)
        self.assertEqual(len(res["examples"]), 1)

    def test_unknown_calibration_flagged(self):
        r1 = dict(ROW_NORMAL, rx=1.0, city="miami",
                  config_version="miami-temperature-v1.0-cal-20261012")
        r2 = dict(ROW_LATE, rx=2.0, city="miami",
                  config_version="miami-temperature-v1.0-cal-20261019")
        tape = M.WxTape([r1, r2], calibrations=cals())
        self.assertEqual(tape.unknown_cal_versions, ["miami-temperature-v1.0-cal-20261019"])
        res = M.replica_check([r1, r2], cals())
        self.assertEqual(res["unknown_config_versions"], ["miami-temperature-v1.0-cal-20261019"])

    def test_replica_check_range_live_stream(self):
        t = ROW_NORMAL["t"] / 1000.0
        rows = [dict(ROW_NORMAL, rx=t + 311.0, city="miami"),
                dict(ROW_DEG4_CAL1012, rx=ROW_DEG4_CAL1012["t"] / 1000.0 + 311.0, city="miami")]
        for r in rows:
            C.append_jsonl(C.raw_file("wx", r["rx"]), r)
        # a pending row of the same minute must not count
        C.append_jsonl(C.raw_file("wx", t + 150), dict(wx_row(ROW_NORMAL["t"], flat_temps(), t + 150)))
        res = M.replica_check_range(t, ROW_DEG4_CAL1012["t"] / 1000.0 + 400, cals())
        self.assertEqual(res["by_status"]["normal"]["n"], 1)
        self.assertEqual(res["by_status"]["degraded"]["exact"], 1)

    def test_replica_check_cli(self):
        d = tempfile.mkdtemp(dir=C.DATA_ROOT)
        with open(os.path.join(d, "detailed_1.json"), "w") as f:
            json.dump({"city": "miami", "timeseries": FIXTURES}, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = M.main(["replica-check", "--dir", d])
        self.assertEqual(rc, 0)
        self.assertIn("normal", buf.getvalue())


class TestTape(unittest.TestCase):
    def test_lookahead_and_partial_minutes(self):
        prev = dict(flat_temps(27), KFLL1M=28)
        cur = dict(flat_temps(27), KFLL1M=29)          # KFLL lands late
        rows = minute_rows(T0, prev) + minute_rows(T0 + 60000, cur)
        tape = M.WxTape(rows, calibrations=cals())
        ts = T0 / 1000.0
        s = tape.snapshot(ts + 145.9)
        self.assertIsNone(s.latest_minute_ms)
        s = tape.snapshot(ts + 146)                     # rx <= at_rx inclusive
        self.assertEqual(s.latest_minute_ms, T0)
        self.assertEqual(s.n_latest, 4)
        # 4-station replica uses offsets; the estimate fills nothing (KFLL never seen)
        self.assertEqual(s.est_latest, s.index_latest)
        s = tape.snapshot(ts + 200)
        self.assertEqual(s.n_latest, 5)
        self.assertAlmostEqual(s.index_latest, C.index_from_sum(4 * 27 + 28))
        # next minute's 4 stations: KFLL filled from its newest reading (28 C)
        s = tape.snapshot(ts + 60 + 146)
        self.assertEqual(s.latest_minute_ms, T0 + 60000)
        self.assertEqual(s.n_latest, 4)
        self.assertAlmostEqual(s.est_latest, (4 * f_of_c(27) + f_of_c(28)) / 5, places=9)
        self.assertEqual(s.stations_latest["KFLL1M"], (T0, f_of_c(28)))
        self.assertNotIn(T0 + 60000, s.full_minutes)
        self.assertIn(T0, s.full_minutes)
        # rows with rx > at_rx are ignored even after a later call (reset)
        s_late = tape.snapshot(ts + 60 + 400)
        self.assertEqual(s_late.index_latest, C.index_from_sum(4 * 27 + 29))
        s_back = tape.snapshot(ts + 146)
        self.assertEqual(s_back.latest_minute_ms, T0)
        self.assertEqual(s_back.n_latest, 4)

    def test_late_station_not_used_as_fill(self):
        temps = dict(flat_temps(27), KFLL1M=30)
        ts = T0 / 1000.0
        rows = [wx_row(T0, temps, ts + 330, status="degraded",
                       v=M.replica_index({s: f_of_c(c) for s, c in temps.items() if s != "KFLL1M"},
                                         cals().for_minute(T0).offsets,
                                         cals().for_minute(T0).b_city_c),
                       codes={"KFLL1M": "late"})]
        rows[0]["v"] = None                      # pretend the official is not out yet
        rows[0]["status"] = "incomplete"
        snap = M.WxTape(rows, calibrations=cals()).snapshot(ts + 400)
        self.assertEqual(snap.n_latest, 4)
        self.assertEqual(snap.est_latest, snap.index_latest)   # late KFLL ignored
        self.assertEqual(snap.stations_latest["KFLL1M"], (T0, f_of_c(30)))

    def test_incremental_equals_fresh(self):
        rng = random.Random(11)
        rows = []
        temps = flat_temps(27)
        for i in range(90):
            for s in M.STATIONS:
                if rng.random() < 0.1:
                    temps[s] += rng.choice((-1, 1))
            rows += minute_rows(T0 + i * 60000, dict(temps))
        rng.shuffle(rows)                                # any order in
        tape = M.WxTape(rows, calibrations=cals())
        times = sorted(T0 / 1000.0 + rng.uniform(0, 95 * 60) for _ in range(150))
        for t in times:
            a = tape.snapshot(t)
            b = M.WxTape(rows, calibrations=cals()).snapshot(t)
            self.assertEqual((a.latest_minute_ms, a.index_latest, a.history, a.stations_latest,
                              a.est_latest, a.n_latest, a.full_minutes),
                             (b.latest_minute_ms, b.index_latest, b.history, b.stations_latest,
                              b.est_latest, b.n_latest, b.full_minutes), t)
        # history keeps at most the last 60 minutes, oldest first
        s = tape.snapshot(T0 / 1000.0 + 95 * 60)
        self.assertEqual(len(s.history), 60)
        self.assertEqual(s.history[0][0], s.latest_minute_ms - 59 * 60000)
        self.assertEqual(s.history[-1][0], s.latest_minute_ms)
        # cached object while nothing new arrives
        self.assertIs(tape.snapshot(T0 / 1000.0 + 95 * 60 + 1), s)

    def test_from_detailed_synthetic_rx(self):
        t = ROW_METAR5["t"]
        tape = M.WxTape.from_detailed({"city": "miami", "timeseries": [ROW_METAR5]},
                                      calibrations=cals())
        first_recv = 1791340518229 / 1000.0
        self.assertIsNone(tape.snapshot(first_recv + 0.99).latest_minute_ms)
        s = tape.snapshot(1791340518890 / 1000.0 + 1.0)   # KMIA, KFXE, KPMP: 3 stations
        self.assertIsNone(s.latest_minute_ms)
        self.assertIn("KMIA1M", s.stations_latest)
        self.assertNotIn("KOPF1M", s.stations_latest)    # METAR fallback waits for t+301
        s = tape.snapshot(1791340603934 / 1000.0 + 1.0)   # KFLL -> 4 stations
        self.assertEqual(s.latest_minute_ms, t)
        self.assertEqual(s.n_latest, 4)
        self.assertNotEqual(s.index_latest, 81.25)        # 4-station replica
        s = tape.snapshot(t / 1000.0 + 301.0)
        self.assertEqual(s.n_latest, 5)
        self.assertIn(t, s.full_minutes)                  # all five in
        self.assertEqual(s.index_latest, 81.25)
        s = tape.snapshot(t / 1000.0 + 311.0)
        self.assertIn(t, s.full_minutes)
        self.assertEqual(s.index_latest, 81.25)

    def test_synth_no_received_and_late(self):
        row = json.loads(json.dumps(ROW_LATE))
        late_recv = row["t"] + 330000                    # after the official v (t+311)
        for s in row["stations"]:
            if s["station_id"] == "KMIA1M":
                del s["received_at_ms"]
            if s["station_id"] == "KFLL1M":
                s["received_at_ms"] = late_recv
        syn = M.synth_rows_from_detailed([row])
        t = row["t"] / 1000.0
        mia_rx = min(r["rx"] for r in syn if any(s["station_id"] == "KMIA1M"
                                                  for s in r["stations"]))
        self.assertAlmostEqual(mia_rx, t + 150.0)
        final = [r for r in syn if r["v"] is not None]
        self.assertAlmostEqual(final[0]["rx"], t + 311.0)
        self.assertNotIn("KFLL1M", [s["station_id"] for s in final[0]["stations"]])
        self.assertIn("KFLL1M", [s["station_id"] for s in final[-1]["stations"]])
        self.assertAlmostEqual(final[-1]["rx"], late_recv / 1000.0 + 1.0)
        tape = M.WxTape(syn, calibrations=cals())
        self.assertEqual(tape.snapshot(t + 400).index_latest, 80.12)

    def test_qc_failed_reading_ignored(self):
        """Sep 1 2026: KMIA reported temp_f -77.8 with code "range" /
        "provider_qc". The synthetic tape relabelled it "pending", so the
        pending replica read 47.48 F and the fill reused -77.8 for minutes:
        those few samples drove the refit's dev coefficient from ~0.25 to
        ~1.2 (a 2.07, b -1.08 at tau 3)."""
        cs = cals()
        t = T0
        cal = cs.for_minute(t)
        v4 = M.replica_index({s: 78.8 for s in ("KFLL1M", "KFXE1M", "KOPF1M", "KPMP1M")},
                             cal.offsets, cal.b_city_c, cal.weights)
        row = {"t": t, "status": "degraded", "v": v4, "contributors": 4, "stations": [
            _st("KFLL1M", 78.8, recv=t + 146000), _st("KFXE1M", 78.8, recv=t + 146000),
            _st("KMIA1M", -77.8, code="range", recv=t + 140000),
            _st("KOPF1M", 78.8, recv=t + 142000), _st("KPMP1M", 78.8, recv=t + 146000)]}
        self.assertAlmostEqual(M.replica_for_row(row, cs, codes=frozenset(("ok",))), v4, places=6)
        syn = M.synth_rows_from_detailed([row])
        self.assertFalse(any(s["station_id"] == "KMIA1M" and s["code"] == "pending"
                             for r in syn for s in r["stations"]))
        tape = M.WxTape(syn, calibrations=cs)
        s = tape.snapshot(t / 1000.0 + 150)
        self.assertEqual(s.n_latest, 4)
        self.assertAlmostEqual(s.index_latest, v4, places=6)
        self.assertNotIn("KMIA1M", s.stations_latest)
        # next minute KMIA "missing": the fill must not reuse -77.8
        nxt = wx_row(t + 60000, {k: 26 for k in M.STATIONS if k != "KMIA1M"}, t / 1000.0 + 206)
        nxt["stations"].append({"station_id": "KMIA1M", "code": "missing"})
        s2 = M.WxTape(syn + [nxt], calibrations=cs).snapshot(t / 1000.0 + 210)
        self.assertEqual(s2.latest_minute_ms, t + 60000)
        self.assertGreater(s2.est_latest, 70.0)
        # a live pending row carrying an impossible reading is ignored too
        live = wx_row(t, dict(flat_temps(27)), t / 1000.0 + 150)
        for st in live["stations"]:
            if st["station_id"] == "KMIA1M":
                st["temp_f"] = -77.8
        s3 = M.WxTape([live], calibrations=cs).snapshot(t / 1000.0 + 150)
        self.assertEqual(s3.n_latest, 4)
        self.assertGreater(s3.index_latest, 79.0)
        self.assertNotIn("KMIA1M", s3.stations_latest)

    def test_load_wx_history_formats(self):
        d = tempfile.mkdtemp(dir=C.DATA_ROOT)
        with open(os.path.join(d, "a.json"), "w") as f:
            json.dump({"fetched_at": 1, "resp": {"timeseries": [ROW_NORMAL, ROW_LATE]}}, f)
        with open(os.path.join(d, "b.jsonl"), "w") as f:
            f.write(json.dumps(dict(ROW_METAR5, rx=1.0, city="miami")) + "\n")
            f.write(json.dumps({"t": 1791340440000, "status": "incomplete", "stations": []}) + "\n")
        rows, files = M.load_wx_history(d)
        self.assertEqual(len(files), 2)
        self.assertEqual([r["t"] for r in rows], sorted([ROW_NORMAL["t"], ROW_LATE["t"],
                                                         ROW_METAR5["t"]]))


class TestFairModel(unittest.TestCase):
    def setUp(self):
        if os.path.exists(C.MODEL_PARAMS_FILE):
            os.remove(C.MODEL_PARAMS_FILE)

    def tearDown(self):
        if os.path.exists(C.MODEL_PARAMS_FILE):
            os.remove(C.MODEL_PARAMS_FILE)

    def _tape(self, minutes=40, c=27, settle_offset_min=None):
        rows = []
        for i in range(minutes):
            rows += minute_rows(T0 + i * 60000, flat_temps(c))
        return M.WxTape(rows, calibrations=cals())

    def test_defaults_when_no_params_file(self):
        fm = M.FairModel()
        self.assertEqual(fm.params_source, "defaults")
        self.assertEqual(len(fm.sig["night"]), len(M.TAU_KNOTS))
        self.assertEqual(fm.tail_w, 0.05)
        self.assertEqual(fm.tail_k, 2.5)
        # a and b from ONE joint regression (agentD b_clim ~0.75, b_trend
        # ~0.04); the trend-only 0.27 next to a=0.75 double-counted the drift
        self.assertEqual(fm.a[0], 0.75)
        self.assertEqual(fm.b[0], 0.04)
        self.assertEqual(tuple(M.DEFAULT_MEAN_COEFS), (0.75, 0.04, 0.0, 0.0))
        # night sigma at tau 60: 1.11 total * 0.55 / mixture inflation
        infl = math.sqrt(0.95 + 0.05 * 6.25)
        self.assertAlmostEqual(fm.sig["night"][-1], round(1.11 * 0.55 / infl, 4))

    def test_params_file_and_bad_params(self):
        P = M.default_params()
        P["a"] = [0.5] * len(P["tau_knots"])
        P["source"] = "fit"
        C.write_json_atomic(C.MODEL_PARAMS_FILE, P)
        fm = M.FairModel()
        self.assertEqual(fm.params_source, C.MODEL_PARAMS_FILE)
        self.assertEqual(fm.a[3], 0.5)
        bad = dict(P, b=[0.1, 0.2])                      # wrong length
        fm2 = M.FairModel(bad)
        self.assertIn("params_warning", fm2.params)
        self.assertEqual(fm2.b[0], 0.04)
        self.assertEqual(fm2.a[0], 0.75)                 # whole knot set reverts together
        old = dict(P)
        old.pop("e")                                     # params from before the anomaly term
        self.assertEqual(M.FairModel(old).e, [0.0] * len(M.TAU_KNOTS))

    def test_empty_snapshot(self):
        fm = M.FairModel()
        snap = M.WxTape([], calibrations=cals()).snapshot(T0 / 1000.0)
        self.assertIsNone(fm.dist(snap, T0 / 1000.0 + 3600))
        self.assertEqual(fm.fair(snap, T0 / 1000.0 + 3600, [80.99]), {})

    def test_monotone_in_strike_and_tau(self):
        fm = M.FairModel()
        tape = self._tape(minutes=70)
        S = (T0 + 60 * 60000) / 1000.0
        strikes = [70.99 + k for k in range(21)]
        for lag in (55, 30, 10, 3):
            snap = tape.snapshot(S - lag * 60 - 30)
            fr = fm.fair(snap, S, strikes)
            ps = [fr[k] for k in strikes]
            self.assertTrue(all(0 < p < 1 for p in ps))
            self.assertTrue(all(a >= b for a, b in zip(ps, ps[1:])), (lag, ps))
            self.assertGreater(ps[0], 0.99)
            self.assertLess(ps[-1], 0.01)
        s_far = fm.dist(tape.snapshot(S - 50 * 60), S)[1]
        s_near = fm.dist(tape.snapshot(S - 5 * 60), S)[1]
        self.assertGreater(s_far, s_near)
        # tau = S - latest minute; data for minute m lands ~2.4-3.5 min after m
        snap = tape.snapshot(S - 30 - 0)
        tau = (S - snap.latest_minute_ms / 1000.0) / 60.0
        self.assertTrue(2.0 <= tau <= 4.5, tau)

    def test_lattice_continuity(self):
        P = M.default_params()
        P["sigma_core"] = {b: [0.01] * len(M.TAU_KNOTS) for b in M.BUCKET_NAMES}
        P["tail_w"] = 0.0
        P["a"] = P["b"] = [0.0] * len(M.TAU_KNOTS)
        fm = M.FairModel(P)
        tape = self._tape(c=27)                          # index 80.6 = 32 + 0.36*135
        S = (T0 + 45 * 60000) / 1000.0
        snap = tape.snapshot(S - 120)
        mu, sd = fm.dist(snap, S)
        self.assertAlmostEqual(mu, 80.6, places=6)
        fr = fm.fair(snap, S, [79.99, 80.59, 80.99])
        self.assertGreater(fr[79.99], 0.999)             # threshold 80.24 <= 80.6
        self.assertGreater(fr[80.59], 0.999)             # threshold exactly 80.60
        self.assertLess(fr[80.99], 0.001)                # 80.96 <= 80.99 -> threshold 81.32
        self.assertEqual(fm.threshold(80.99), 81.32)
        self.assertEqual(fm.threshold(80.59), 80.6)

    def test_tau_le_zero_deterministic(self):
        fm = M.FairModel()
        rows = []
        for i in range(40):
            rows += minute_rows(T0 + i * 60000, flat_temps(27))
        S_ms = T0 + 30 * 60000
        tape = M.WxTape(rows, calibrations=cals())
        S = S_ms / 1000.0
        strikes = [79.99, 80.59, 80.6, 80.99]
        for at in (S + 200, S + 311, S + 600):            # minute S known with 5 stations
            snap = tape.snapshot(at)
            self.assertGreaterEqual(snap.latest_minute_ms, S_ms)
            mu, sd = fm.dist(snap, S)
            self.assertEqual(sd, 0.0)
            self.assertEqual(mu, 80.6)
            fr = fm.fair(snap, S, strikes)
            self.assertEqual(fr, {k: (1.0 if C.yes_wins(k, 80.6) else 0.0) for k in strikes})
        # only 4 stations of minute S so far: near strikes stay uncertain
        snap = M.WxTape(rows, calibrations=cals()).snapshot(S + 150)
        self.assertEqual(snap.latest_minute_ms, S_ms)
        mu, sd = fm.dist(snap, S)
        self.assertGreater(sd, 0.0)
        p = fm.fair(snap, S, [80.59, 70.99, 90.99])
        self.assertTrue(0.0 < p[80.59] < 1.0)
        self.assertGreater(p[70.99], 0.9999)
        self.assertLess(p[90.99], 0.0001)

    def test_vol_multiplier(self):
        P = M.default_params()
        P.update(vol_gamma=0.5, vol_gamma_short=1.0)
        fm = M.FairModel(P)
        self.assertEqual(fm.vol_mult(3, "night", None, None), 1.0)
        ref60, ref20 = fm.rv_ref["night"], fm.rv20_ref["night"]
        self.assertAlmostEqual(fm.vol_mult(3, "night", ref60, ref20), 1.0)
        hi = fm.vol_mult(3, "night", ref60, 4 * ref20)
        self.assertGreater(hi, 1.0)
        self.assertAlmostEqual(fm.vol_mult(15, "night", ref60, 4 * ref20), 1.0)  # short term off
        self.assertAlmostEqual(fm.vol_mult(30, "night", 0.0, None),
                               max(fm.vol_lo, ((0.05) / (ref60 + 0.05)) ** 0.5))
        self.assertEqual(fm.vol_mult(30, "night", 100.0, None), fm.vol_hi)   # clipped

    def test_foreign_snapshot_object(self):
        """Duck-typed snapshots without the extras still work (contract fields only)."""
        from types import SimpleNamespace
        fm = M.FairModel()
        m = T0 + 30 * 60000
        snap = SimpleNamespace(latest_minute_ms=m, index_latest=80.6,
                               history=[(m - i * 60000, 80.6) for i in range(15, -1, -1)],
                               stations_latest={})
        S = m / 1000.0 + 600
        mu, sd = fm.dist(snap, S)
        self.assertTrue(math.isfinite(mu) and sd > 0)
        self.assertEqual(fm.dist(snap, m / 1000.0), (80.6, 0.0))

    def _zero_mean_model(self, sd=0.05):
        P = M.default_params()
        n = len(M.TAU_KNOTS)
        P["a"], P["b"], P["c"], P["e"] = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
        P["sigma_core"] = {b: [sd] * n for b in M.BUCKET_NAMES}
        P["tail_w"] = 0.0
        return M.FairModel(P)

    def test_four_station_basis_lattice(self):
        """Review 10/15: a station out longer than the fill age leaves a
        4-station basis (0.45 F lattice shifted by offsets); the cut must sit
        on that lattice, not the five-station one."""
        cs = cals()
        T1 = 1791871800000                                  # 2026-10-13, cal-20261012
        temps = {"KMIA1M": 28, "KOPF1M": 28, "KFXE1M": 28, "KPMP1M": 27}
        cal = cs.for_minute(T1)
        v4 = M.replica_index({s: f_of_c(c) for s, c in temps.items()}, cal.offsets,
                             cal.b_city_c, cal.weights)
        self.assertEqual(v4, 81.94)
        rows = []
        for i in range(60):                                 # KFLL missing all hour
            t = T1 + i * 60000
            r = wx_row(t, temps, t / 1000.0 + 144)
            r["stations"].append({"station_id": "KFLL1M", "code": "missing"})
            rows.append(r)
            f = wx_row(t, temps, t / 1000.0 + 311, status="degraded", v=v4)
            f["stations"].append({"station_id": "KFLL1M", "code": "missing"})
            rows.append(f)
        S = (T1 + 50 * 60000) / 1000.0
        snap = M.WxTape(rows, calibrations=cs).snapshot(S - 1)
        self.assertEqual(snap.n_latest, 4)
        self.assertEqual(snap.est_latest, 81.94)            # nothing to fill with
        base, step, p0, keep_tau = snap.lattice
        self.assertAlmostEqual(step, 0.45, places=12)
        self.assertEqual((p0, keep_tau), M.LATTICE_KEEP["outage"])
        fm = self._zero_mean_model()
        self.assertAlmostEqual(fm.cut(81.99, snap.lattice), 82.165, places=9)
        self.assertAlmostEqual(fm.cut(81.99), 81.86, places=9)   # five-station cut
        mu, sd, lat = fm.dist_lattice(snap, S)
        self.assertAlmostEqual(mu, 81.94, places=9)
        tau = (S - snap.latest_minute_ms / 1000.0) / 60.0
        self.assertAlmostEqual(tau, 3.0)
        q = p0 * math.exp(-tau / keep_tau)
        self.assertAlmostEqual(lat[2], q, places=12)
        p = fm.fair(snap, S, [81.49, 81.99])
        # the index has sat at 81.94 (< 81.99) all hour: mostly NO now; the
        # five-station cut (81.86) alone priced it a 94% YES
        p_def = fm.prob_above(mu, sd, 81.99)
        self.assertGreater(p_def, 0.9)
        self.assertLess(p[81.99], 0.1)
        self.assertAlmostEqual(p[81.99], q * fm.prob_above(mu, sd, 81.99, lat[:2])
                               + (1 - q) * p_def, places=9)
        self.assertEqual(p[81.99], fm.prob_above(mu, sd, 81.99, lat))
        self.assertGreater(p[81.49], 0.99)                  # 81.94 > 81.49
        # further out the outage may end: more weight on the default lattice
        lat60 = fm.dist_lattice(snap, S + 57 * 60)[2]
        self.assertLess(lat60[2], q)
        # KFLL only lagging (filled from its last reading): default lattice
        rows2 = []
        for i in range(10):
            rows2 += minute_rows(T0 + i * 60000, dict(flat_temps(27), KFLL1M=28))
        snap2 = M.WxTape(rows2, calibrations=cs).snapshot(T0 / 1000.0 + 9 * 60 + 150)
        self.assertEqual(snap2.n_latest, 4)
        self.assertIsNone(snap2.lattice)
        # KFLL out 7 min (past the 5-min fill age, short of 10): a short gap
        # usually ends before S -> default lattice
        rows3 = []
        for i in range(20):
            t = T0 + i * 60000
            if i < 12:
                rows3 += minute_rows(t, dict(flat_temps(27), KFLL1M=28))
            else:                                           # KFLL absent from minute 12 on
                four = {k: 27 for k in M.STATIONS if k != "KFLL1M"}
                rows3.append(wx_row(t, four, t / 1000.0 + 146))
        snap3 = M.WxTape(rows3, calibrations=cs).snapshot((T0 + 18 * 60000) / 1000.0 + 150)
        self.assertEqual((snap3.latest_minute_ms - (T0 + 11 * 60000)) // 60000, 7)
        self.assertEqual(snap3.n_latest, 4)
        self.assertEqual(snap3.est_latest, snap3.index_latest)   # no fill
        self.assertIsNone(snap3.lattice)

    def test_basis_lattice_values(self):
        cs = cals()
        cal = cs.for_minute(ROW_NORMAL["t"])
        self.assertIsNone(M.basis_lattice(M.row_stations(ROW_NORMAL), cal))
        # METAR fallback (tenths of C): five stations, shifted 0.36 F lattice
        cal = cs.for_minute(ROW_METAR5["t"])
        base, step = M.basis_lattice(M.row_stations(ROW_METAR5), cal)
        self.assertAlmostEqual(step, 0.36, places=12)
        self.assertAlmostEqual(base, ROW_METAR5["v"], delta=0.005)
        fm = M.FairModel()
        self.assertAlmostEqual(fm.cut(80.99, (base, step)), 81.25 - 0.18, places=6)
        self.assertAlmostEqual(fm.cut(81.25, (base, step)), 81.25 + 0.18, places=6)
        # the default lattice reproduces threshold() - 0.18
        for k in (70.99, 79.99, 80.59, 81.99, 90.99):
            lat = (C.index_from_sum(140), C.STEP_F)
            self.assertAlmostEqual(fm.cut(k, lat), fm.cut(k), places=9)
        self.assertAlmostEqual(fm.cut(80.99, (80.0, 0.0)), 80.995)   # no lattice

    def test_density_and_slopes(self):
        P = M.default_params()
        P.update(tail_w=0.2, tail_k=2.0)
        fm = M.FairModel(P)
        mu, sd = 80.0, 0.25
        # density integrates to the mixture CDF
        a, b, n = 79.7, 80.4, 4000
        h = (b - a) / n
        integ = sum(fm.density(mu, sd, a + (i + 0.5) * h) for i in range(n)) * h
        self.assertAlmostEqual(integ, fm.mixcdf((b - mu) / sd) - fm.mixcdf((a - mu) / sd), places=6)
        # at the mean: 0.9x the plain normal of the core sigma (0.8 + 0.2/2)
        phi0 = 1 / math.sqrt(2 * math.pi)
        self.assertAlmostEqual(fm.density(mu, sd, mu) / (phi0 / sd), 0.9, places=9)
        # at z = 3 the tails make it 0.8 + 0.1 e^3.375 = 3.72x the plain normal
        z3 = math.exp(-4.5) * phi0 / sd
        self.assertAlmostEqual(fm.density(mu, sd, mu + 3 * sd) / z3,
                               0.8 + 0.1 * math.exp(3.375), places=9)
        self.assertEqual(fm.density(mu, 0.0, mu), 0.0)
        tape = self._tape(minutes=40)
        S = (T0 + 45 * 60000) / 1000.0
        snap = tape.snapshot(S - 600)
        m2, s2 = fm.dist(snap, S)
        sl = fm.slopes(snap, S, [79.99, 80.59, 80.99])
        for k, g in sl.items():
            self.assertAlmostEqual(g, 100 * fm.density(m2, s2, fm.cut(k)) * C.STEP_F, places=9)
        # dP/dmu by finite difference matches density at the cut
        k, eps = 80.59, 1e-5
        dp = (fm.prob_above(m2 + eps, s2, k) - fm.prob_above(m2 - eps, s2, k)) / (2 * eps)
        self.assertAlmostEqual(dp * 100 * C.STEP_F, sl[k], places=4)
        self.assertEqual(fm.slopes(M.WxTape([], calibrations=cals()).snapshot(0), S, [80.99]), {})

    def test_vol_features_ignore_composition(self):
        """Review 10/15: rv/rv20 read the latest minute's 4-station replica
        (KFLL lags ~97% of the time); flat temperatures must give zero vol."""
        P = M.default_params()
        P.update(vol_gamma=0.5, vol_gamma_short=1.0)
        fm = M.FairModel(P)
        rows = []
        temps = dict(flat_temps(28), KFLL1M=29)
        for i in range(60):
            rows += minute_rows(T0 + i * 60000, temps)
        tape = M.WxTape(rows, calibrations=cals())
        L = T0 + 55 * 60000
        snap4 = tape.snapshot(L / 1000.0 + 150)              # L has 4 stations
        self.assertEqual((snap4.latest_minute_ms, snap4.n_latest), (L, 4))
        self.assertNotEqual(snap4.index_latest, snap4.est_latest)   # replica != estimate
        f4 = dict(fm.features(snap4))
        snap5 = tape.snapshot(L / 1000.0 + 201)              # KFLL in
        self.assertEqual(snap5.n_latest, 5)
        f5 = dict(fm.features(snap5))
        for k in ("rv", "rv20", "trend", "dev"):
            self.assertAlmostEqual(f4[k], 0.0, places=9, msg=k)
            self.assertAlmostEqual(f5[k], 0.0, places=9, msg=k)
        S = (L + 3 * 60000) / 1000.0
        self.assertAlmostEqual(fm.dist(snap4, S)[1], fm.dist(snap5, S)[1], places=12)

    def test_dist_mean_terms(self):
        P = M.default_params()
        n = len(M.TAU_KNOTS)
        P["a"], P["b"], P["c"], P["e"] = [0.0] * n, [0.0] * n, [0.0] * n, [0.0] * n
        fm0 = M.FairModel(P)
        rows = []
        for i in range(40):                              # rising 1 C per 8 min at KMIA
            temps = flat_temps(27)
            temps["KMIA1M"] = 27 + i // 8
            rows += minute_rows(T0 + i * 60000, temps)
        tape = M.WxTape(rows, calibrations=cals())
        S = (T0 + 60 * 60000) / 1000.0
        snap = tape.snapshot((T0 + 39 * 60000) / 1000.0 + 320)
        self.assertEqual(fm0.dist(snap, S)[0], snap.est_latest)
        f = fm0.features(snap)
        self.assertGreater(f["trend"], 0.0)
        P2 = dict(P, b=[1.0] * n)
        mu_tr = M.FairModel(P2).dist(snap, S)[0]
        tau = (S - snap.latest_minute_ms / 1000.0) / 60.0
        self.assertAlmostEqual(mu_tr - snap.est_latest, f["trend"] * tau, places=9)
        P3 = dict(P, a=[1.0] * n)
        fm3 = M.FairModel(P3)
        self.assertAlmostEqual(fm3.dist(snap, S)[0] - snap.est_latest, fm3.drift(S, tau), places=9)


def _synthetic_history(t0_ms, minutes, seed=5):
    """Detailed minute rows: whole-C stations around a diurnal curve."""
    rng = random.Random(seed)
    base = dict(zip(M.STATIONS, (0.4, -0.3, 0.1, 0.0, -0.2)))
    walk = 0.0
    rows = []
    for i in range(minutes):
        t = t0_ms + i * 60000
        mod = M.est_minute_of_day(t / 1000.0)
        diurnal = 2.5 * math.sin(2 * math.pi * (mod - 9 * 60) / 1440.0)
        walk = 0.98 * walk + rng.gauss(0, 0.05)
        temps = {s: int(round(26.5 + base[s] + diurnal + walk + rng.gauss(0, 0.2)))
                 for s in M.STATIONS}
        sts = []
        for s, c in temps.items():
            lag = 205000 if s == "KFLL1M" else 145000 + rng.randint(0, 3000)
            sts.append(_st(s, f_of_c(c), recv=t + lag))
        rows.append({"t": t, "status": "normal", "contributors": 5,
                     "v": C.index_from_sum(sum(temps.values())), "stations": sts})
    return rows


def _write_iem(path, t0_ms, minutes):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["valid_utc", "MIA", "OPF", "FLL", "FXE", "PMP", "n", "index_c", "index_f"])
        for i in range(minutes):
            t = t0_ms + i * 60000
            mod = M.est_minute_of_day(t / 1000.0)
            x = 80 + 4.5 * math.sin(2 * math.pi * (mod - 9 * 60) / 1440.0)
            xf = round(x * 5) / 5
            w.writerow([C.iso(t / 1000.0)[:16] + "Z", 80, 80, 80, 80, 80, 5,
                        round((xf - 32) / 1.8, 4), xf])


class TestFitHelpers(unittest.TestCase):
    def test_n_eff_factor(self):
        self.assertEqual(M.n_eff_factor(60, 2), 2 / 60)
        self.assertEqual(M.n_eff_factor(3, 2), 2 / 3)
        self.assertEqual(M.n_eff_factor(1, 2), 1.0)          # no overlap below the step

    def _sm(self, n, knot, a_true, seed=4):
        import numpy as np
        rng = np.random.default_rng(seed)
        D = rng.normal(0.0, 1.0, n)
        return {"knot": np.full(n, float(knot)), "D": D, "trend": rng.normal(0, 0.01, n),
                "tau": np.full(n, float(knot)), "dev": rng.normal(0, 0.1, n),
                "anom": rng.normal(0, 0.3, n), "i0": np.zeros(n),
                "y": a_true * D + rng.normal(0, 0.5, n)}

    def test_ridge_weighs_effective_samples(self):
        """Review 10/15: ridge_n prior pseudo-samples were weighed against the
        overlapping-target count; at knot 60 / step 2 that is 30x too weak."""
        sm = self._sm(600, 60, a_true=1.4)
        c_eff, m_eff = M.fit_mean_coefs(sm, ridge_n=50.0, step_min=2)
        c_nom, m_nom = M.fit_mean_coefs(sm, ridge_n=50.0, step_min=2, ridge_neff=False)
        a_prior = M.DEFAULT_MEAN_COEFS[0]
        self.assertEqual(m_eff["60"]["n_eff"], 20.0)
        self.assertEqual(m_nom["60"]["n_eff"], 600.0)
        self.assertGreater(c_nom[60][0], 1.25)                 # ~OLS
        self.assertLess(c_eff[60][0], c_nom[60][0])
        self.assertGreater(c_eff[60][0], a_prior)              # between prior and OLS
        self.assertLess(abs(c_eff[60][0] - a_prior), abs(c_nom[60][0] - a_prior))

    def test_bootstrap_event_clusters(self):
        rng = random.Random(2)
        rows = []
        for ev in range(40):                                   # model always a bit better
            for _ in range(20):
                y = rng.random() < 0.5
                rows.append((0.6 if y else 0.4, 0.55 if y else 0.45, 1 if y else 0, ev))
        rows.append((0.5, None, 1, 99))                        # no market: skipped
        b = M._bootstrap(rows, n_boot=500)
        self.assertEqual((b["n_events"], b["n"]), (40, 800))
        mm = b["brier_model_minus_mkt"]
        self.assertLess(mm["hi"], 0.0)
        self.assertEqual(mm["p_neg"], 1.0)
        self.assertLessEqual(mm["lo"], mm["mean"])
        self.assertLessEqual(mm["mean"], mm["hi"])
        # a coin-flip edge, perfectly correlated within events: wide CI around 0
        rows2 = []
        for ev in range(30):
            better = ev % 2 == 0
            for _ in range(20):
                rows2.append((0.7 if better else 0.3, 0.5, 1, ev))
        b2 = M._bootstrap(rows2, n_boot=500)["brier_model_minus_mkt"]
        self.assertLess(b2["lo"], 0.0)
        self.assertGreater(b2["hi"], 0.0)
        self.assertEqual(M._bootstrap(rows2[:5])["n_events"], 1)


class TestFitEval(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(dir=C.DATA_ROOT)
        cls.t0 = 1791158400000                           # 2026-10-05 00:00Z
        cls.rows = _synthetic_history(cls.t0, 4 * 1440)
        cls.wx_dir = os.path.join(cls.dir, "wx")
        os.makedirs(cls.wx_dir)
        with open(os.path.join(cls.wx_dir, "detailed_a.json"), "w") as f:
            json.dump({"city": "miami", "timeseries": cls.rows}, f)
        cls.iem = os.path.join(cls.dir, "iem.csv")
        _write_iem(cls.iem, cls.t0 - 9 * 86400000, 9 * 1440)
        # settled events on the last day, markets around the realized value
        cls.settled = os.path.join(cls.dir, "settled")
        os.makedirs(cls.settled)
        finals = {r["t"]: r["v"] for r in cls.rows}
        rng = random.Random(9)
        for h in range(78, 95):
            S_ms = cls.t0 + h * 3600000
            v = finals[S_ms]
            ev = "KXTEMPMIAH-" + C.et_dt(S_ms / 1000.0).strftime("%y%b%d%H").upper()
            markets, trades = [], {}
            for k in range(int(v) - 3, int(v) + 3):
                tk = f"{ev}-T{k}.99"
                res = "yes" if C.yes_wins(k + 0.99, v) else "no"
                markets.append({"ticker": tk, "event_ticker": ev, "floor_strike": k + 0.99,
                                "result": res, "expiration_value": str(v),
                                "close_time": C.iso(S_ms / 1000.0)})
                trades[tk] = [{"created_time": C.iso(S_ms / 1000.0 - sec),
                               "yes_price_dollars": f"{min(0.99, max(0.01, (0.8 if res == 'yes' else 0.2) + rng.uniform(-0.15, 0.15))):.4f}",
                               "count_fp": "5.00", "taker_side": "yes"}
                              for sec in (3500, 2000, 900, 400, 100)]
            with open(os.path.join(cls.settled, f"{ev}.json"), "w") as f:
                json.dump({"event_ticker": ev, "markets": markets, "trades": trades}, f)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)
        if os.path.exists(C.MODEL_PARAMS_FILE):
            os.remove(C.MODEL_PARAMS_FILE)

    def test_fit_drift_shape(self):
        iem, _ = M.load_iem(self.iem)
        table, n = M.fit_drift(iem)
        self.assertEqual(sorted(table, key=int), [str(m) for m in range(1, 13)])
        self.assertTrue(all(len(v) == 24 for v in table.values()))
        sep, oct_ = table["9"], table["10"]
        self.assertAlmostEqual(sum(oct_), 0.0, delta=0.05)  # closure
        self.assertGreater(oct_[6], 0)                        # sin peaks at 15 EST
        self.assertLess(oct_[20], 0)
        self.assertGreater(n["10"] + n["9"], 1000)

    def test_fit_cli_writes_params(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = M.main(["fit", "--wx-dir", self.wx_dir, "--iem", self.iem, "--step", "10"])
        self.assertEqual(rc, 0)
        P = C.read_json(C.MODEL_PARAMS_FILE)
        self.assertEqual(P["source"], "fit")
        n = len(P["tau_knots"])
        for k in ("a", "b", "c", "e"):
            self.assertEqual(len(P[k]), n)
        for b in M.BUCKET_NAMES:
            self.assertEqual(len(P["sigma_core"][b]), n)
            self.assertTrue(all(x > 0 for x in P["sigma_core"][b]))
        self.assertGreater(P["fit"]["wx"]["n_samples"], 1000)
        self.assertIn("iem", P["fit"])
        # Review 10/15 (major): each (knot, bucket) sigma is shrunk toward the
        # knot's pooled Kalshi MLE by EFFECTIVE samples n * min(1, step/knot),
        # not by the overlapping-target count
        cells, pooled = P["fit"]["sigma_cells"], P["fit"]["sigma_pooled"]
        n0 = P["fit"]["sigma_shrink"]["n0"]
        self.assertEqual(P["fit"]["sigma_shrink"]["mode"], "neff_pooled")
        self.assertEqual(P["fit"]["sigma_shrink"]["step_min"], 10)
        self.assertTrue(cells)
        for key, c in cells.items():
            kn = int(key.split("|")[0])
            self.assertAlmostEqual(c["n_eff"], c["n"] * min(1.0, 10.0 / kn), delta=0.06, msg=key)
            self.assertAlmostEqual(c["prior"], pooled[str(kn)], delta=1e-4, msg=key)
            want = (c["n_eff"] * c["mle"] + n0 * c["prior"]) / (c["n_eff"] + n0)
            self.assertAlmostEqual(c["final"], want, delta=2e-4, msg=key)
        self.assertLess(cells["60|afternoon"]["n_eff"], cells["60|afternoon"]["n"] / 5)
        for kn, cm in P["fit"]["mean_coefs"].items():
            self.assertAlmostEqual(cm["n_eff"], cm["n"] * min(1.0, 10.0 / int(kn)), delta=0.06)
        fm = M.FairModel()
        self.assertEqual(fm.params_source, C.MODEL_PARAMS_FILE)
        # re-runnable: --until restricts the history
        with contextlib.redirect_stdout(io.StringIO()):
            M.main(["fit", "--wx-dir", self.wx_dir, "--iem", self.iem, "--step", "10",
                    "--until", "2026-10-07"])
        P2 = C.read_json(C.MODEL_PARAMS_FILE)
        self.assertLess(P2["fit"]["wx"]["n_samples"], P["fit"]["wx"]["n_samples"])
        self.assertEqual(P2["fit"]["until_ms"], int(C.et_day_bounds("2026-10-07")[0] * 1000))

    def test_neff_shrinks_thin_cells_to_pooled(self):
        """A bucket whose nominal n is large but whose n_eff is small (long
        knot, overlapping targets) ends near the pooled level; the legacy rule
        kept it near its own MLE."""
        with contextlib.redirect_stdout(io.StringIO()):
            Pn = M.fit_params(self.rows, iem_path=self.iem, step_min=10, cals=M.Calibrations(None))
            Pl = M.fit_params(self.rows, iem_path=self.iem, step_min=10, cals=M.Calibrations(None),
                              sigma_shrink="legacy")
        cn, cl = Pn["fit"]["sigma_cells"], Pl["fit"]["sigma_cells"]
        pooled = Pn["fit"]["sigma_pooled"]
        c = cn["60|afternoon"]
        w_new = c["n_eff"] / (c["n_eff"] + 200.0)
        w_old = cl["60|afternoon"]["n"] / (cl["60|afternoon"]["n"] + 200.0)
        self.assertLess(w_new, 0.5 * w_old)
        if abs(c["mle"] - pooled["60"]) > 1e-3:
            self.assertLess(abs(c["final"] - pooled["60"]), abs(c["mle"] - pooled["60"]))

    def test_default_split(self):
        rows = [{"t": self.t0}, {"t": self.t0 + 20 * 86400000}]
        # history alone: first ET midnight >= 40% of 20 days (= 8 days) in
        self.assertEqual(M.default_split(rows), "2026-10-13")
        ev = [{"settle_ts": self.t0 / 1000.0 + 10 * 86400}]
        self.assertEqual(M.default_split(rows, ev), "2026-10-14")
        early = [{"settle_ts": self.t0 / 1000.0 + 86400}]   # < 3 days of history before it
        self.assertEqual(M.default_split(rows, early), "2026-10-13")

    def test_eval_smoke(self):
        with contextlib.redirect_stdout(io.StringIO()):
            res = M.run_eval(split_day="2026-10-08", wx_dir=self.wx_dir,
                             settled_dirs=[self.settled], iem_path=self.iem, step_min=10)
        self.assertGreater(res["events_eval"], 5)
        fitted = res["models"]["fitted"]
        self.assertGreater(fitted["overall"]["n"], 100)
        self.assertIn("brier_mkt", fitted["by_tau"]["30"])
        self.assertTrue(res["written"].startswith(C.DATA_ROOT))
        self.assertTrue(os.path.exists(res["written"]))
        # the time split is honest: the fit saw only minutes before the split
        split_ms = int(C.et_day_bounds("2026-10-08")[0] * 1000)
        self.assertLessEqual(C.parse_iso(res["fit_meta"]["wx"]["to"]) * 1000, split_ms)
        self.assertGreaterEqual(C.parse_iso(res["eval_range"][0]) * 1000, split_ms)
        # event-clustered CIs and per-bucket calibration (review 10/15)
        bs = fitted["bootstrap"]
        self.assertGreater(bs["overall"]["n_events"], 5)     # events with a market price
        self.assertLessEqual(bs["overall"]["n_events"], res["events_eval"])
        for key in ("brier_model_minus_mkt", "brier_blend_minus_mkt", "ll_model_minus_mkt"):
            d = bs["overall"][key]
            self.assertLessEqual(d["lo"], d["hi"])
        self.assertIn("30", bs["by_tau"])
        cb = res["calibration_by_bucket"]
        self.assertTrue(any(cb[b] for b in M.BUCKET_NAMES))
        for row in cb.values():
            for v in row.values():
                self.assertGreater(v["n"], 0)
                self.assertTrue(0.0 < v["z_rms"] < 10.0)


if __name__ == "__main__":
    unittest.main()
