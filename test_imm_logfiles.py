"""Tests for imm_logfiles: plain and gzip day files read the same (R-261124-03)."""
import gzip
import os
import tempfile
import time
import unittest

import imm_logfiles as lf


def write_gz_like_retention(path: str) -> str:
    """Compress `path` the way imm_log_retention does: header name + mtime,
    the .gz given the original's mtime, the original removed."""
    st = os.stat(path)
    gz = path + ".gz"
    with open(path, "rb") as src, open(gz, "wb") as raw:
        with gzip.GzipFile(filename=os.path.basename(path), mode="wb", fileobj=raw,
                           mtime=int(st.st_mtime)) as g:
            g.write(src.read())
    os.utime(gz, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.remove(path)
    return gz


class LogFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name: str, text: str, mtime: float = None) -> str:
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if mtime is not None:
            os.utime(p, (mtime, mtime))
        return p

    def test_day_files_lists_plain_and_gz_sorted(self):
        self.put("cycle_log_2026-10-02.csv", "b\n")
        gz = write_gz_like_retention(self.put("cycle_log_2026-10-01.csv", "a\n"))
        self.put("cycle_log_2026-10-03.csv", "c\n")
        self.put("cycle_log_backup.csv", "x\n")             # not a day file
        self.put("cycle_log_2026-10-04.csv.gz.tmp", "x")     # a retention temp file
        self.put("orders_2026-10-01.jsonl", "{}\n")         # another prefix
        got = lf.day_files(self.dir, "cycle_log", "csv")
        self.assertEqual([d for d, _p in got], ["2026-10-01", "2026-10-02", "2026-10-03"])
        self.assertEqual(got[0][1], gz)
        self.assertEqual(lf.day_files(self.dir, "cycle_log", "csv", "2026-10-02", "2026-10-02"),
                         [("2026-10-02", os.path.join(self.dir, "cycle_log_2026-10-02.csv"))])

    def test_plain_wins_when_both_exist(self):
        p = self.put("marks_2026-10-01.jsonl", "{}\n")
        with gzip.open(p + ".gz", "wb") as g:
            g.write(b"{}\n")
        self.assertEqual(lf.day_files(self.dir, "marks", "jsonl"), [("2026-10-01", p)])

    def test_prefix_is_exact(self):
        self.put("selection_snapshot_2026-10-01.jsonl", "{}\n")
        self.put("selection_events_2026-10-01.jsonl", "{}\n")
        self.assertEqual(len(lf.day_files(self.dir, "selection_snapshot", "jsonl")), 1)
        self.assertEqual(lf.day_files(self.dir, "selection", "jsonl"), [])

    def test_missing_directory_is_empty(self):
        self.assertEqual(lf.day_files(os.path.join(self.dir, "nope"), "cycle_log", "csv"), [])

    def test_open_text_and_bin_read_both_forms(self):
        text = "ts,ticker\r\n2026-10-01T00:00:00Z,KX-1\n"
        p = self.put("cycle_log_2026-10-01.csv", text)
        with lf.open_text(p, newline="") as f:
            plain = f.read()
        with lf.open_bin(p) as f:
            plain_b = f.read()
        gz = write_gz_like_retention(p)
        with lf.open_text(gz, newline="") as f:
            self.assertEqual(f.read(), plain)
        with lf.open_bin(gz) as f:
            self.assertEqual(f.read(), plain_b)
        with lf.open_text(gz, encoding="utf-8", errors="replace") as f:
            self.assertEqual(sum(1 for _ in f), 2)

    def test_sig_and_size_carry_over_a_compression(self):
        p = self.put("cycle_log_2026-10-01.csv", "x" * 5000, mtime=time.time() - 40 * 86400)
        before_sig, before_size = lf.sig(p), lf.orig_size(p)
        gz = write_gz_like_retention(p)
        self.assertEqual(lf.sig(gz), before_sig)
        self.assertEqual(lf.orig_size(gz), before_size)
        self.assertLess(os.path.getsize(gz), before_size)
        self.assertEqual(lf.plain_name(gz), "cycle_log_2026-10-01.csv")
        self.assertEqual(lf.plain_path(gz), p)

    def test_resolve(self):
        p = os.path.join(self.dir, "cycle_log_2026-10-01.csv")
        self.assertEqual(lf.resolve(p), p)                   # neither: unchanged
        self.put("cycle_log_2026-10-01.csv", "a\n")
        self.assertEqual(lf.resolve(p), p)                   # plain
        gz = write_gz_like_retention(p)
        self.assertEqual(lf.resolve(p), gz)                  # only the .gz

    def test_isize_rejects_non_gzip(self):
        p = self.put("x.csv.gz", "not gzip")
        with self.assertRaises(OSError):
            lf.gz_isize(p)


if __name__ == "__main__":
    unittest.main()
