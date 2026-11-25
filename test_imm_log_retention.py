"""Tests for imm_log_retention (review R-261124-03; Jack 2026-11-25: "Yes, 35 days").

The job may only ever lose data on purpose, so most of what is asserted is
what it must NOT do: compress a file a reader still rewrites, touch a type the
bot reads, overwrite a .gz, or drop the original before the copy is proven."""
import gzip
import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import imm_log_retention as ret
import imm_logfiles as lf

NOW = datetime(2026, 11, 27, 7, 40, tzinfo=timezone.utc).timestamp()   # 02:40 ET
OLD = datetime(2026, 10, 2, 4, 0, tzinfo=timezone.utc).timestamp()     # last write of an old day


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "incentive-mm")
        self.arch = os.path.join(self.dir, "dashboard", "cache", "day_archive")
        self.out = os.path.join(self.dir, "retention")
        os.makedirs(self.arch)
        for d in ("2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03"):
            self.archive(d)
        env = mock.patch.dict(os.environ, {"IMM_LOG_RETENTION": "1"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def archive(self, day):
        with open(os.path.join(self.arch, f"{day}.json"), "w") as f:
            f.write("{}")

    def put(self, name, text="x,y\n" * 500, mtime=OLD):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.utime(p, (mtime, mtime))
        return p

    def run_job(self, **kw):
        kw.setdefault("status_dir", self.dir)
        kw.setdefault("archive_dir", self.arch)
        kw.setdefault("now", NOW)
        kw.setdefault("today_local", "2026-11-27")
        with mock.patch("builtins.print"):
            return ret.run(**kw)

    def last_run(self):
        with open(os.path.join(self.out, "last_run.json"), encoding="utf-8") as f:
            return json.load(f)

    def manifest(self):
        try:
            with open(os.path.join(self.out, "manifest.jsonl"), encoding="utf-8") as f:
                return [json.loads(x) for x in f]
        except OSError:
            return []

    # ---- what is chosen --------------------------------------------------------

    def test_scan_takes_only_old_allowlisted_days(self):
        self.put("cycle_log_2026-10-02.csv")
        self.put("orders_2026-10-02.jsonl")
        self.put("cycle_log_2026-10-23.csv")          # 35 days old: not past the cutoff
        self.put("fills_2026-10-02.jsonl")            # the bot reads fills: never
        self.put("settlements_2026-10-02.jsonl")
        self.put("realized_2026-10-02.jsonl")
        self.put("toxic_halts_2026-10-02.jsonl")      # not on the allowlist
        self.put("imm_state.json")
        todo, held = ret.scan(self.dir, self.arch, now=NOW)
        self.assertEqual(sorted(r["name"] for r in todo),
                         ["cycle_log_2026-10-02.csv", "orders_2026-10-02.jsonl"])
        self.assertEqual(held, [])

    def test_cutoff_is_the_dashboard_history_plus_five(self):
        self.assertEqual(ret.CUTOFF_DAYS, ret.HISTORY_DAYS + 5)
        with mock.patch.dict(os.environ, {"IMM_DASH_HISTORY_DAYS": "40"}):
            m = importlib.reload(ret)
            self.assertEqual(m.CUTOFF_DAYS, 45)
        importlib.reload(ret)
        self.assertEqual(ret.CUTOFF_DAYS, ret.HISTORY_DAYS + 5)
        self.archive("2026-10-19")
        self.archive("2026-10-20")
        self.put("cycle_log_2026-10-20.csv")           # 38 days old
        self.assertEqual(len(ret.scan(self.dir, self.arch, now=NOW)[0]), 1)
        self.assertEqual(ret.scan(self.dir, self.arch, now=NOW, cutoff_days=45)[0], [])

    def test_archive_gate_holds_a_day_the_dashboard_has_not_archived(self):
        # UTC 2026-10-05 holds the evening of ET 10-04 and most of ET 10-05
        self.archive("2026-10-05")
        self.put("cycle_log_2026-10-05.csv")
        todo, held = ret.scan(self.dir, self.arch, now=NOW)
        self.assertEqual(todo, [])
        self.assertEqual(held[0]["reason"], "ET day not in the day archive: 2026-10-04")

    def test_no_archive_at_all_holds_everything(self):
        self.put("cycle_log_2026-10-02.csv")
        todo, held = ret.scan(self.dir, os.path.join(self.dir, "missing"), now=NOW)
        self.assertEqual((todo, len(held)), ([], 1))

    def test_bot_logs_go_by_last_write_not_name(self):
        self.put("incentive-mm-2026-09-20.log", mtime=OLD)           # written 10/02: 56 days
        self.put("incentive-mm-2026-09-21.log", mtime=NOW - 20 * 86400)   # an old name still recent
        self.put("incentive-mm-2026-11-25.log", mtime=NOW - 60)
        todo, held = ret.scan(self.dir, self.arch, now=NOW)
        self.assertEqual([r["name"] for r in todo], ["incentive-mm-2026-09-20.log"])
        self.assertEqual(held, [])

    def test_a_file_written_in_the_last_two_days_is_held(self):
        self.put("marks_2026-10-02.jsonl", mtime=NOW - 3600)
        todo, held = ret.scan(self.dir, self.arch, now=NOW)
        self.assertEqual(todo, [])
        self.assertEqual(held[0]["reason"], "written in the last 2 days")

    # ---- one file ----------------------------------------------------------------

    def test_compress_file_keeps_bytes_mtime_and_sig(self):
        p = self.put("cycle_log_2026-10-02.csv", "ts,ticker\r\n" + "2026-10-02T00:00:00Z,KX-1\n" * 2000)
        with open(p, "rb") as f:
            raw = f.read()
        sig, mtime_ns = lf.sig(p), os.stat(p).st_mtime_ns
        res = ret.compress_file(p)
        self.assertEqual(res["action"], "compressed")
        self.assertFalse(os.path.exists(p))
        gz = p + ".gz"
        with gzip.open(gz, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertEqual(os.stat(gz).st_mtime_ns, mtime_ns)
        self.assertEqual(lf.sig(gz), sig)
        self.assertLess(res["gz_size"], len(raw))
        self.assertFalse(os.path.exists(gz + ".tmp"))

    def test_a_failed_verify_keeps_the_original(self):
        p = self.put("orders_2026-10-02.jsonl")
        real = ret._digest
        with mock.patch.object(ret, "_digest", side_effect=lambda path, opener=open: (
                (0, "bad") if path.endswith(".tmp") else real(path, opener))):
            res = ret.compress_file(p)
        self.assertEqual(res["action"], "failed")
        self.assertTrue(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".gz"))
        self.assertFalse(os.path.exists(p + ".gz.tmp"))

    def test_both_present_and_identical_removes_the_plain_copy(self):
        p = self.put("marks_2026-10-02.jsonl")
        with open(p, "rb") as f, gzip.open(p + ".gz", "wb") as g:
            g.write(f.read())
        self.assertEqual(ret.compress_file(p)["action"], "removed_plain")
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.exists(p + ".gz"))

    def test_a_different_gz_is_never_overwritten(self):
        p = self.put("marks_2026-10-02.jsonl")
        with gzip.open(p + ".gz", "wb") as g:
            g.write(b"something else")
        before = os.path.getsize(p + ".gz")
        self.assertEqual(ret.compress_file(p)["action"], "conflict")
        self.assertTrue(os.path.exists(p))
        self.assertEqual(os.path.getsize(p + ".gz"), before)

    # ---- a run -------------------------------------------------------------------

    def test_dry_run_changes_nothing(self):
        p = self.put("cycle_log_2026-10-02.csv")
        self.assertEqual(self.run_job(), 0)
        self.assertTrue(os.path.exists(p))
        self.assertFalse(os.path.exists(p + ".gz"))
        lr = self.last_run()
        self.assertEqual((lr["mode"], lr["candidates"], lr["compressed"]), ("dry-run", 1, 0))
        with open(os.path.join(self.out, "dry_run.txt"), encoding="ascii") as f:
            self.assertIn("compress cycle_log_2026-10-02.csv", f.read())
        self.assertEqual(self.manifest(), [])

    def test_apply_compresses_and_writes_the_manifest(self):
        p = self.put("cycle_log_2026-10-02.csv")
        q = self.put("selection_snapshot_2026-10-02.jsonl")
        f = self.put("fills_2026-10-02.jsonl")
        self.assertEqual(self.run_job(apply=True), 0)
        self.assertFalse(os.path.exists(p) or os.path.exists(q))
        self.assertTrue(os.path.exists(p + ".gz") and os.path.exists(q + ".gz"))
        self.assertTrue(os.path.exists(f) and not os.path.exists(f + ".gz"))
        rows = self.manifest()
        self.assertEqual(sorted(r["file"] for r in rows),
                         ["cycle_log_2026-10-02.csv", "selection_snapshot_2026-10-02.jsonl"])
        self.assertTrue(all(r["action"] == "compressed" and len(r["sha1"]) == 40 for r in rows))
        lr = self.last_run()
        self.assertEqual((lr["mode"], lr["compressed"], lr["failed"]), ("apply", 2, []))
        self.assertGreater(lr["freed_bytes"], 0)
        self.assertEqual(lr["in_bytes"], sum(r["size"] for r in rows))
        self.assertFalse(os.path.exists(os.path.join(self.out, "run.lock")))

    def test_kill_switch_and_stop_file(self):
        p = self.put("cycle_log_2026-10-02.csv")
        with mock.patch.dict(os.environ, {"IMM_LOG_RETENTION": "0"}):
            self.assertEqual(self.run_job(apply=True), 0)
        self.assertEqual(self.last_run()["mode"], "off")
        os.makedirs(self.out, exist_ok=True)
        open(os.path.join(self.out, "STOP"), "w").close()
        self.assertEqual(self.run_job(apply=True), 0)
        self.assertEqual(self.last_run()["mode"], "off")
        self.assertTrue(os.path.exists(p))

    def test_not_before_keeps_apply_a_dry_run(self):
        p = self.put("cycle_log_2026-10-02.csv")
        self.assertEqual(self.run_job(apply=True, not_before="2026-11-28"), 0)
        lr = self.last_run()
        self.assertEqual(lr["mode"], "dry-run")
        self.assertIn("waits until 2026-11-28", lr["reason"])
        self.assertTrue(os.path.exists(p))
        self.assertEqual(self.run_job(apply=True, not_before="2026-11-27"), 0)
        self.assertFalse(os.path.exists(p))

    def test_apply_refuses_while_a_reader_is_not_ready(self):
        p = self.put("cycle_log_2026-10-02.csv")
        code = os.path.join(self.tmp.name, "code")
        os.makedirs(code)
        for name in ret.READERS:
            with open(os.path.join(code, name), "w") as f:
                f.write("import imm_logfiles\n" if name != "imm_dashboard.py" else "import glob\n")
        self.assertEqual(self.run_job(apply=True, code_dir=code), 3)
        lr = self.last_run()
        self.assertEqual(lr["mode"], "refused")
        self.assertIn("imm_dashboard.py", lr["reason"])
        self.assertTrue(os.path.exists(p))
        self.assertEqual(ret.readers_missing(), [])     # this checkout's readers are ready

    def test_the_cap_stops_after_the_first_file_over_it(self):
        for d in ("2026-10-01", "2026-10-02", "2026-10-03"):
            self.put(f"orders_{d}.jsonl", "y" * 4000)
        self.assertEqual(self.run_job(apply=True, max_gb=5000 / ret.GB), 0)
        lr = self.last_run()
        self.assertEqual((lr["compressed"], lr["remaining"]), (1, 2))
        self.assertEqual(lr["stopped"], f"the {5000 / ret.GB:g} GB cap")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "orders_2026-10-01.jsonl.gz")))

    def test_leftover_tmp_is_removed_and_a_lock_blocks_a_second_run(self):
        self.put("cycle_log_2026-10-02.csv")
        junk = self.put("cycle_log_2026-10-01.csv.gz.tmp", "half")
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, "run.lock"), "w") as f:
            f.write("123")
        self.assertEqual(self.run_job(apply=True), 4)
        self.assertTrue(os.path.exists(junk))
        os.remove(os.path.join(self.out, "run.lock"))
        self.assertEqual(self.run_job(apply=True), 0)
        self.assertFalse(os.path.exists(junk))

    @unittest.skipUnless(os.name == "nt", "the held-open check is the Windows sharing rule")
    def test_a_file_held_open_is_skipped(self):
        p = self.put("cycle_log_2026-10-02.csv")
        with open(p, "rb"):
            self.assertTrue(ret.held_open(p))
            self.assertEqual(self.run_job(apply=True), 0)
            self.assertTrue(os.path.exists(p))
            self.assertEqual(self.last_run()["skipped"], {"held open": 1})
        self.assertFalse(ret.held_open(p))

    # ---- undo --------------------------------------------------------------------

    def test_restore_gives_the_original_back(self):
        p = self.put("cycle_log_2026-10-02.csv", "a,b\n" * 3000)
        with open(p, "rb") as f:
            raw = f.read()
        mtime_ns = os.stat(p).st_mtime_ns
        self.run_job(apply=True)
        with mock.patch("builtins.print"):
            self.assertEqual(ret.restore([], all_=True, status_dir=self.dir), 0)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), raw)
        self.assertEqual(os.stat(p).st_mtime_ns, mtime_ns)
        self.assertFalse(os.path.exists(p + ".gz"))
        self.assertEqual(self.manifest()[-1]["action"], "restored")

    def test_restore_refuses_bytes_the_manifest_does_not_know(self):
        p = self.put("cycle_log_2026-10-02.csv")
        self.run_job(apply=True)
        rows = self.manifest()
        rows[0]["sha1"] = "0" * 40
        with open(os.path.join(self.out, "manifest.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(rows[0]) + "\n")
        with mock.patch("builtins.print"):
            self.assertEqual(ret.restore([p + ".gz"], status_dir=self.dir), 1)
        self.assertTrue(os.path.exists(p + ".gz"))
        self.assertFalse(os.path.exists(p))

    def test_cli_refuses_a_lower_cutoff(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            ret.main(["--cutoff-days", str(ret.CUTOFF_DAYS - 1), "--status-dir", self.dir])


if __name__ == "__main__":
    unittest.main()
