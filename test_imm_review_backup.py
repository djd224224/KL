"""Tests for imm_review_backup.py (review R-261124-20): the moat's BigQuery
backup. A fake stands in for BigQuery; every test builds a synthetic run-logs
tree in a temp dir. Nothing calls Google, reads or writes the live tree."""

import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import imm_review as rv
import imm_review_backup as bk


class FakeBQ:
    """The BQ wrapper's methods over in-memory tables (rows as BigQuery stores them)."""

    def __init__(self, problem: str = ""):
        self.tables = {}
        self.problem = problem
        self.loads = []

    def dataset_problem(self):
        return self.problem

    def exists(self, t):
        return t in self.tables

    def line_stats(self, t):
        rows = self.tables[t]
        return len(rows), max((r["line"] for r in rows), default=0)

    def line_at(self, t, n):
        return next((r["raw"] for r in self.tables[t] if r["line"] == n), None)

    def newest_shas(self):
        out = {}
        for r in self.tables["files"]:          # appended in time order: the last per path is the newest
            out[r["path"]] = r["sha256"]
        return out

    def day_shas(self):
        return {r["day"]: r["sha256"] for r in self.tables["day_archive"]}

    def load(self, t, rows, schema, truncate):
        rows = [json.loads(json.dumps(r)) for r in rows]
        self.loads.append((t, len(rows), truncate))
        if truncate or t not in self.tables:
            self.tables[t] = []
        self.tables[t].extend(rows)
        return len(rows)

    def count(self, t):
        return len(self.tables[t]) if t in self.tables else None

    def read_lines(self, t):
        return sorted((r["line"], r["raw"]) for r in self.tables[t])

    def read_newest_files(self):
        newest = {}
        for r in self.tables.get("files", []):
            newest[r["path"]] = r
        return list(newest.values())

    def read_days(self):
        return list(self.tables["day_archive"])


LEDGER = '{"op":"create","id":"R-1","day":"2026-11-24"}\n{"op":"seen","id":"R-1","day":"2026-11-25"}\n'


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.status = os.path.join(self.root, "run-logs", "incentive-mm")
        self.review = os.path.join(self.status, "daily_review")
        os.makedirs(os.path.join(self.status, "dashboard", "cache", "day_archive"))
        rv.configure(status_dir=self.status, review_dir=self.review, repo=self.root)
        self.env = mock.patch.dict(os.environ, {"IMM_REVIEW_BQ_DATASET": "imm_review_test"})
        self.env.start()
        self.bq = FakeBQ()
        self.write("daily_review/ledger.jsonl", LEDGER)
        self.write("daily_review/moat/family_daily.csv", "day,family,net\n2026-11-24,A,1\n")
        self.write("daily_review/2026-11-25/review.md", "# V\n")
        self.write("daily_review/archive_totals_cache.json", "{}")                 # a cache: skipped
        self.write("daily_review/uncommitted_backups/x.py", "print(1)\n")           # skipped
        self.write("program_history.jsonl", '{"date":"2026-11-25","series":"KXA","total_reward_raw":1}\n')
        self.write("dashboard/cache/day_archive/2026-11-24.json", '{"m":{}}')
        self.write("daily_review/config_store/ab.gz", bytes(range(256)))            # binary

    def tearDown(self):
        self.env.stop()
        rv.configure()
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel, data):
        p = os.path.join(self.status, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def run_(self):
        return bk.run(bq=self.bq)

    def test_first_run_copies_everything(self):
        st = self.run_()
        self.assertTrue(st["ok"], st)
        self.assertEqual(st["added"]["lines"]["ledger_lines"], 2)
        self.assertEqual(st["added"]["lines"]["program_history_lines"], 1)
        self.assertEqual(st["added"]["lines"]["changes_lines"], 0)                # no file, no table: quiet
        self.assertEqual(st["added"]["files"], 3)                                 # family_daily, review.md, ab.gz
        self.assertEqual(st["added"]["days_changed"], 1)
        paths = {r["path"] for r in self.bq.tables["files"]}
        self.assertNotIn("daily_review/ledger.jsonl", paths)                       # a log: line by line instead
        self.assertNotIn("daily_review/archive_totals_cache.json", paths)
        row = self.bq.tables["ledger_lines"][1]
        self.assertEqual((row["line"], row["op"], row["id"], row["day"]), (2, "seen", "R-1", "2026-11-25"))
        self.assertEqual(self.bq.tables["program_history_lines"][0]["id"], "KXA")
        state = rv._load_json(os.path.join(self.review, bk.STATE_NAME))
        self.assertTrue(state["ok"])
        self.assertEqual(state["totals"]["ledger_lines"], 2)

    def test_a_second_run_adds_only_what_changed(self):
        self.run_()
        st = self.run_()
        self.assertEqual((sum(st["added"]["lines"].values()), st["added"]["files"], st["added"]["days_changed"]),
                         (0, 0, 0))
        self.write("daily_review/ledger.jsonl", LEDGER + '{"op":"close","id":"R-1"}\n')
        self.write("daily_review/moat/family_daily.csv", "day,family,net\n2026-11-24,A,2\n")
        self.write("dashboard/cache/day_archive/2026-11-25.json", '{"m":{"a":1}}')
        st = self.run_()
        self.assertEqual((st["added"]["lines"]["ledger_lines"], st["added"]["files"], st["added"]["days_changed"]),
                         (1, 1, 1))
        self.assertEqual(len(self.bq.tables["day_archive"]), 2)                    # replaced as a whole set
        self.assertEqual(len(self.bq.tables["files"]), 4)                          # the old version stays

    def test_a_partial_last_line_waits(self):
        self.run_()
        self.write("daily_review/ledger.jsonl", LEDGER + '{"op":"cl')
        self.assertEqual(self.run_()["added"]["lines"]["ledger_lines"], 0)
        self.write("daily_review/ledger.jsonl", LEDGER + '{"op":"close"}\n')
        self.assertEqual(self.run_()["added"]["lines"]["ledger_lines"], 1)

    def test_a_truncated_or_rewritten_log_is_refused(self):
        self.run_()
        self.write("daily_review/ledger.jsonl", LEDGER.splitlines(True)[0])
        st = self.run_()
        self.assertFalse(st["ok"])
        self.assertIn("truncated or replaced", st["refused"][0])
        self.assertEqual(len(self.bq.tables["ledger_lines"]), 2)                   # the copy keeps every line
        self.write("daily_review/ledger.jsonl", LEDGER.splitlines(True)[0] + '{"op":"other"}\n')
        st = self.run_()
        self.assertIn("rewritten, not appended", st["refused"][0])
        os.remove(os.path.join(self.review, "ledger.jsonl"))
        self.assertIn("gone locally", self.run_()["refused"][0])

    def test_a_reverted_file_gets_a_new_version(self):
        self.run_()
        fd = "daily_review/moat/family_daily.csv"
        self.write(fd, "B\n")
        self.run_()
        self.write(fd, "day,family,net\n2026-11-24,A,1\n")                          # back to the first content
        self.assertEqual(self.run_()["added"]["files"], 1)
        self.assertEqual(self.bq.newest_shas()["daily_review/" + fd[len("daily_review/"):]],
                         bk._sha(b"day,family,net\n2026-11-24,A,1\n"))

    def test_a_lost_day_is_refused(self):
        self.run_()
        os.remove(os.path.join(self.status, "dashboard", "cache", "day_archive", "2026-11-24.json"))
        st = self.run_()
        self.assertIn("gone locally", st["refused"][0])
        self.assertEqual(len(self.bq.tables["day_archive"]), 1)

    def test_a_dataset_problem_stops_everything(self):
        self.bq = FakeBQ(problem="dataset x not found")
        st = self.run_()
        self.assertFalse(st["ok"])
        self.assertEqual((st["errors"], self.bq.loads), (["dataset x not found"], []))

    def test_the_off_switch(self):
        with mock.patch.dict(os.environ, {"IMM_REVIEW_BQ": "0"}):
            st = self.run_()
        self.assertTrue(st["ok"] and st["off"])
        self.assertEqual(self.bq.loads, [])

    def test_a_non_live_folder_needs_its_own_dataset(self):
        # the live repo is elsewhere: this folder is a scratch copy (the launcher's -NoEmail tests)
        rv.configure(status_dir=self.status, review_dir=self.review, repo=os.path.join(self.root, "KL"))
        with mock.patch.dict(os.environ, {"IMM_REVIEW_BQ_DATASET": ""}):
            st = self.run_()
        self.assertFalse(st["ok"])
        self.assertIn("not the live one", st["errors"][0])
        self.assertEqual(self.bq.loads, [])
        self.assertFalse(os.path.exists(os.path.join(self.review, bk.STATE_NAME)))

    def test_dry_run_loads_nothing(self):
        st = bk.run(dry=True, bq=self.bq)
        self.assertEqual((st["added"]["lines"]["ledger_lines"], self.bq.loads), (2, []))
        self.assertFalse(os.path.exists(os.path.join(self.review, bk.STATE_NAME)))

    def test_restore_round_trip_and_verify(self):
        # the box writes its logs CRLF: a restore must give the same bytes back
        self.write("program_history.jsonl", '{"date":"2026-11-25","series":"KXA"}\r\n{"date":"2026-11-26"}\r\n')
        self.run_()
        self.assertEqual(self.bq.tables["program_history_lines"][0]["id"], "KXA")   # parsed through the \r
        self.assertTrue(bk.verify(bq=self.bq)["ok"])
        out_dir = os.path.join(self.root, "restored")
        out = bk.restore(out_dir, bq=self.bq)
        self.assertEqual(out["bad_sha"], [])
        for rel in ("daily_review/ledger.jsonl", "program_history.jsonl", "daily_review/moat/family_daily.csv",
                    "daily_review/2026-11-25/review.md", "daily_review/config_store/ab.gz",
                    "dashboard/cache/day_archive/2026-11-24.json"):
            with open(os.path.join(self.status, *rel.split("/")), "rb") as a, \
                    open(os.path.join(out_dir, *rel.split("/")), "rb") as b:
                self.assertEqual(a.read(), b.read(), rel)
        with self.assertRaises(SystemExit):
            bk.restore(os.path.join(self.status, "x"), bq=self.bq)               # never into the live run-logs
        self.write("daily_review/moat/family_daily.csv", "changed\n")
        v = bk.verify(bq=self.bq)
        self.assertFalse(v["ok"])
        self.assertIn("family_daily.csv", v["problems"][0])


if __name__ == "__main__":
    unittest.main()
