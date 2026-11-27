"""Tests for kl_sync_automerge.py and the sync's auto-merge step (R-261125-02).

Every test builds scratch repositories in a temp folder: a bare "GitHub", the
box's clone, and a second clone that pushes the way the high-temp digest does.
No test touches the live KL repository or imports the bot. The end-to-end tests
run sync_kl_main.ps1 itself with -Repo set to the scratch box. For any folder
but the live one, the script stops before its machine-wide steps (scheduled
tasks, the bot restart request).
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

import kl_sync_automerge as am

HERE = os.path.dirname(os.path.abspath(__file__))
LIVE = os.path.normcase(r"C:\Users\jackd\Documents\KL")


def git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {p.stderr.strip()}")
    return p.stdout.strip()


def git_rc(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, text=True).returncode


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _writable(func, path, *_):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class Repos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kl-automerge-")
        self.assertNotEqual(os.path.normcase(os.path.abspath(self.tmp)), LIVE)
        self.origin = os.path.join(self.tmp, "github.git")
        self.box = os.path.join(self.tmp, "box")
        self.digest = os.path.join(self.tmp, "digest")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.box], check=True)
        self.cfg(self.box)
        self.write(self.box, "bot.py", "print('bot')\n")
        self.write(self.box, "run-logs/high-temp/latest.html", "<p>11/24</p>\n")
        self.commit(self.box, "seed")
        git(self.box, "remote", "add", "origin", self.origin)
        git(self.box, "push", "-q", "-u", "origin", "main")
        # core.autocrlf goes in before the checkout: under this machine's global
        # autocrlf=true the clone would write CRLF files, and every digest
        # commit would then carry bot.py too
        subprocess.run(["git", "clone", "-q", "-c", "core.autocrlf=false", self.origin, self.digest],
                       check=True)
        self.cfg(self.digest)
        self.assertEqual(git(self.digest, "status", "--porcelain"), "")
        self.env = mock.patch.dict(os.environ, {"KL_SYNC_AUTO_MERGE": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, onerror=_writable)

    def cfg(self, repo):
        for k, v in (("user.name", "Test"), ("user.email", "test@example.invalid"),
                     ("core.autocrlf", "false"), ("commit.gpgsign", "false")):
            git(repo, "config", k, v)

    def path(self, repo, rel):
        return os.path.join(repo, *rel.split("/"))

    def write(self, repo, rel, text):
        p = self.path(repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

    def commit(self, repo, msg):
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", msg)
        return git(repo, "rev-parse", "HEAD")

    def digest_push(self, files, msg="High-temp digest for 2026-11-25"):
        for rel, text in files.items():
            self.write(self.digest, rel, text)
        self.commit(self.digest, msg)
        git(self.digest, "push", "-q", "origin", "main")
        git(self.box, "fetch", "-q", "origin")

    def box_commit(self, rel="bot.py", text="print('bot v2')\n", msg="imm: a change only the box has"):
        self.write(self.box, rel, text)
        return self.commit(self.box, msg)

    def diverge(self):
        """The 11/26 night: an unpushed commit on the box, then the digest on GitHub."""
        self.box_commit()
        self.digest_push({"run-logs/high-temp/2026-11-25.html": "<p>11/25</p>\n",
                          "run-logs/high-temp/latest.html": "<p>11/25</p>\n"})

    def head(self):
        return git(self.box, "rev-parse", "HEAD")


class MergeRule(Repos):
    def test_merges_the_digest_into_a_box_that_is_ahead(self):
        self.diverge()
        self.write(self.box, "bot.py", "print('bot v3, uncommitted')\n")   # a session's WIP
        self.write(self.box, "wip.py", "half done\n")                        # untracked WIP
        bot = self.path(self.box, "bot.py")
        bot_mtime = os.path.getmtime(bot)
        before, up = self.head(), git(self.box, "rev-parse", "origin/main")
        code, line = am.merge(self.box)
        self.assertEqual(code, 0, line)
        self.assertIn("merged 1 run-logs-only commit(s) from origin/main", line)
        self.assertIn("the box is 2 commit(s) ahead of GitHub", line)
        m = self.head()
        self.assertEqual(git(self.box, "rev-list", "--parents", "-n", "1", m).split()[1:], [before, up])
        self.assertEqual(git(self.box, "rev-list", "--count", "HEAD..origin/main"), "0")
        self.assertIn("[KL sync auto-merge]", git(self.box, "log", "-1", "--format=%s"))
        self.assertEqual(read(self.path(self.box, "run-logs/high-temp/2026-11-25.html")), "<p>11/25</p>\n")
        self.assertEqual(read(self.path(self.box, "run-logs/high-temp/latest.html")), "<p>11/25</p>\n")
        # the bot's file and the WIP are untouched: no restart, nothing swept up
        self.assertEqual(read(bot), "print('bot v3, uncommitted')\n")
        self.assertEqual(os.path.getmtime(bot), bot_mtime)
        self.assertEqual(read(self.path(self.box, "wip.py")), "half done\n")
        self.assertEqual(git(self.box, "show", "HEAD:bot.py"), "print('bot v2')")
        # a second run has nothing to do
        code, line = am.merge(self.box)
        self.assertEqual((code, line), (1, "not behind origin/main (nothing to merge)"))

    def test_refuses_code_from_github(self):
        self.box_commit()
        self.digest_push({"bot.py": "print('pushed elsewhere')\n"}, msg="imm: code pushed from elsewhere")
        before = self.head()
        code, line = am.merge(self.box)
        self.assertEqual(code, 1)
        self.assertEqual(line, "1 incoming commit(s) touch 1 path(s) outside run-logs/: bot.py")
        self.assertEqual(self.head(), before)

    def test_refuses_if_any_incoming_commit_touched_code_even_when_undone(self):
        self.box_commit()
        self.digest_push({"run-logs/high-temp/2026-11-25.html": "<p>11/25</p>\n"})
        self.digest_push({"other.py": "x = 1\n"}, msg="add other.py")
        os.remove(self.path(self.digest, "other.py"))
        self.commit(self.digest, "remove other.py")
        git(self.digest, "push", "-q", "origin", "main")
        git(self.box, "fetch", "-q", "origin")
        code, line = am.merge(self.box)
        self.assertEqual(code, 1)
        self.assertIn("outside run-logs/: other.py", line)

    def test_refuses_code_renamed_into_run_logs(self):
        self.box_commit(rel="notes.txt", text="box\n")
        os.makedirs(self.path(self.digest, "run-logs/moved"), exist_ok=True)
        git(self.digest, "mv", "bot.py", "run-logs/moved/bot.py")
        self.commit(self.digest, "move the bot")
        git(self.digest, "push", "-q", "origin", "main")
        git(self.box, "fetch", "-q", "origin")
        code, line = am.merge(self.box)
        self.assertEqual(code, 1)
        self.assertIn("outside run-logs/: bot.py", line)

    def test_refuses_a_conflict_and_leaves_no_merge_behind(self):
        self.box_commit(rel="run-logs/high-temp/latest.html", text="<p>box edit</p>\n")
        self.digest_push({"run-logs/high-temp/latest.html": "<p>11/25</p>\n"})
        before = self.head()
        code, line = am.merge(self.box)
        self.assertEqual(code, 1)
        self.assertEqual(line, "the merge conflicts in run-logs/high-temp/latest.html")
        self.assertEqual(self.head(), before)
        self.assertNotEqual(git_rc(self.box, "rev-parse", "-q", "--verify", "MERGE_HEAD"), 0)
        self.assertEqual(read(self.path(self.box, "run-logs/high-temp/latest.html")), "<p>box edit</p>\n")
        self.assertEqual(git(self.box, "status", "--porcelain"), "")

    def test_refuses_an_incoming_file_edited_here(self):
        self.diverge()
        self.write(self.box, "run-logs/high-temp/latest.html", "<p>hand edit</p>\n")
        before = self.head()
        code, line = am.merge(self.box)
        self.assertEqual(code, 1)
        self.assertIn("modified, untracked or ignored here: run-logs/high-temp/latest.html", line)
        self.assertEqual(read(self.path(self.box, "run-logs/high-temp/latest.html")), "<p>hand edit</p>\n")
        self.assertEqual(self.head(), before)

    def test_refuses_an_untracked_file_in_the_way(self):
        self.box_commit()
        self.write(self.box, "run-logs/high-temp/2026-11-25.html", "<p>local copy</p>\n")
        self.digest_push({"run-logs/high-temp/2026-11-25.html": "<p>11/25</p>\n"})
        code, line = am.merge(self.box)
        self.assertEqual(code, 1)
        self.assertIn("run-logs/high-temp/2026-11-25.html", line)
        self.assertEqual(read(self.path(self.box, "run-logs/high-temp/2026-11-25.html")), "<p>local copy</p>\n")

    def test_refuses_an_ignored_file_in_the_way(self):
        # git would overwrite an ignored file without a word; the rule refuses
        self.box_commit(rel=".gitignore", text="*.tmp\n", msg="ignore tmp files")
        self.write(self.box, "run-logs/high-temp/a.tmp", "local\n")
        self.digest_push({"run-logs/high-temp/a.tmp": "pushed\n"})
        code, line = am.merge(self.box)
        self.assertEqual(code, 1)
        self.assertIn("run-logs/high-temp/a.tmp", line)
        self.assertEqual(read(self.path(self.box, "run-logs/high-temp/a.tmp")), "local\n")

    def test_refuses_with_something_staged(self):
        self.diverge()
        self.write(self.box, "staged.py", "x = 1\n")
        git(self.box, "add", "staged.py")
        code, line = am.merge(self.box)
        self.assertEqual(code, 1)
        self.assertIn("something is staged", line)

    def test_off_switches(self):
        self.diverge()
        before = self.head()
        with mock.patch.dict(os.environ, {"KL_SYNC_AUTO_MERGE": "0"}):
            self.assertEqual(am.merge(self.box), (1, "off (KL_SYNC_AUTO_MERGE=0)"))
        self.write(self.box, "run-logs/sync-auto-merge.off", "")
        self.assertEqual(am.merge(self.box), (1, "off (run-logs/sync-auto-merge.off exists)"))
        self.assertEqual(self.head(), before)

    def test_refuses_off_main(self):
        self.diverge()
        git(self.box, "checkout", "-q", "-b", "side")
        self.assertEqual(am.merge(self.box), (1, "not on main (side)"))

    def test_needs_a_real_divergence(self):
        self.digest_push({"run-logs/high-temp/2026-11-25.html": "<p>11/25</p>\n"})
        self.assertEqual(am.merge(self.box), (1, "not ahead of origin/main (a plain fast-forward applies)"))
        git(self.box, "merge", "-q", "--ff-only", "origin/main")
        self.box_commit()
        self.assertEqual(am.merge(self.box), (1, "not behind origin/main (nothing to merge)"))

    def test_net_zero_run_logs_commits_still_merge(self):
        self.box_commit()
        self.digest_push({"run-logs/high-temp/tmp.html": "x\n"})
        os.remove(self.path(self.digest, "run-logs/high-temp/tmp.html"))
        self.commit(self.digest, "drop tmp.html")
        git(self.digest, "push", "-q", "origin", "main")
        git(self.box, "fetch", "-q", "origin")
        code, line = am.merge(self.box)
        self.assertEqual(code, 0, line)
        self.assertIn("(no file changes)", line)
        self.assertEqual(git(self.box, "rev-list", "--count", "HEAD..origin/main"), "0")

    def test_dry_run_changes_nothing(self):
        self.diverge()
        before = self.head()
        code, line = am.merge(self.box, dry=True)
        self.assertEqual(code, 0)
        self.assertEqual(line, "would merge 1 run-logs-only commit(s) from origin/main "
                               "(run-logs/high-temp/2026-11-25.html, run-logs/high-temp/latest.html)")
        self.assertEqual(self.head(), before)
        self.assertFalse(os.path.exists(self.path(self.box, "run-logs/high-temp/2026-11-25.html")))

    def test_cli_prints_one_line_and_has_no_default_repo(self):
        self.diverge()
        script = os.path.join(HERE, "kl_sync_automerge.py")
        p = subprocess.run([sys.executable, script, "--repo", self.box, "--dry-run"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(p.stdout.strip().splitlines()), 1)
        self.assertTrue(p.stdout.isascii())
        p = subprocess.run([sys.executable, script], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)


@unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell.exe"), "Windows PowerShell only")
class SyncScriptEndToEnd(Repos):
    """sync_kl_main.ps1 itself, against the scratch box, as Task Scheduler runs it."""

    def run_sync(self):
        shutil.copy(os.path.join(HERE, "kl_sync_automerge.py"), self.path(self.box, "kl_sync_automerge.py"))
        self.assertFalse(os.path.exists(self.path(self.box, "restart_imm.ps1")))
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        os.path.join(HERE, "sync_kl_main.ps1"), "-Repo", self.box],
                       capture_output=True, text=True, timeout=900)
        with open(self.path(self.box, "run-logs/sync-kl-main.log"), encoding="utf-8-sig") as fh:
            return [l for l in fh.read().splitlines() if l.strip()]

    def review_status(self):
        import imm_review as rv
        with mock.patch.object(rv, "RUN_LOGS", os.path.join(self.box, "run-logs")):
            return rv.sync_status(date.today())

    def test_a_digest_night_logs_one_clean_auto_merge(self):
        self.diverge()
        lines = self.run_sync()
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(" AUTO-MERGED: merged 1 run-logs-only commit(s) from origin/main", lines[0])
        self.assertNotIn(" !", lines[0])
        self.assertEqual(git(self.box, "rev-list", "--count", "HEAD..origin/main"), "0")
        st = self.review_status()
        self.assertEqual(st["failure_streak"], 0)
        self.assertEqual(st["last_success"], st["last_run"])

    def test_code_on_githubs_side_still_fails_loudly(self):
        self.box_commit()
        self.digest_push({"bot.py": "print('pushed elsewhere')\n"}, msg="imm: code pushed from elsewhere")
        before = self.head()
        lines = self.run_sync()
        self.assertEqual(self.head(), before)
        text = "\n".join(lines)
        self.assertIn("! MERGE FAILED (exit 128)", text)
        self.assertIn("! AUTO-MERGE refused (exit 1): 1 incoming commit(s) touch 1 path(s) "
                      "outside run-logs/: bot.py", text)
        self.assertIn("! BEHIND origin", text)
        self.assertEqual(self.review_status()["failure_streak"], 1)


if __name__ == "__main__":
    unittest.main()
