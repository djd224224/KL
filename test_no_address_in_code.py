#!/usr/bin/env python3
"""No file in the repo holds the account's email address (Jack 2026-11-26).

Why: the Claude desktop app's privacy check refuses any command that runs or
names a script holding that address beside a client name (a User-Agent), or
putting it in a URL -- which is how incentive_mm.py and its suite came to be
runnable only by Jack. And the repo is public. So:
  * alerts and reports default to the sending Gmail (ALERT_EMAIL_FROM), which
    is the inbox; IMM_ALERT_TO / CMM_ALERT_TO / PF_DIGEST_TO / ... still
    override it;
  * User-Agents are plain names; the one site that refuses one (bls.gov, in
    imm_release_calendar) adds KL_UA_CONTACT, which Jack sets himself;
  * the GitHub workflows send ALERT_EMAIL_TO from the ALERT_EMAIL_FROM secret.
The address is read from the environment here, never written in this file,
and a failure names only the files.

Run: python -m unittest test_no_address_in_code
"""

import os
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_PREFIXES = ("run-logs/", ".claude/worktrees/")


def _address() -> str:
    v = os.environ.get("ALERT_EMAIL_FROM", "")
    if not v:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, "ALERT_EMAIL_FROM")[0] or "")
        except (OSError, ImportError):
            v = ""
    return v.strip().lower()


def _repo_files() -> list:
    """Tracked files plus untracked ones git does not ignore."""
    out = []
    for args in (["ls-files", "-z"], ["ls-files", "-z", "--others", "--exclude-standard"]):
        raw = subprocess.run(["git", *args], cwd=HERE, capture_output=True, check=True).stdout
        out += [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    return [p for p in out if not p.startswith(SKIP_PREFIXES)]


def holders(addr: str, files, root: str = HERE) -> list:
    needle = addr.encode()
    found = []
    for p in files:
        try:
            with open(os.path.join(root, p), "rb") as f:
                if needle in f.read().lower():
                    found.append(p)
        except OSError:
            continue
    return sorted(found)


class TestNoAddressInCode(unittest.TestCase):

    def test_no_repo_file_holds_the_address(self):
        addr = _address()
        if "@" not in addr:
            self.skipTest("ALERT_EMAIL_FROM is not set on this machine")
        found = holders(addr, _repo_files())
        self.assertEqual(found, [], "the account's address is back in these files; read it "
                                    "from ALERT_EMAIL_FROM (or KL_UA_CONTACT for a site that "
                                    "needs a contact) instead")

    def test_the_scan_finds_a_planted_address(self):
        import tempfile
        d = tempfile.mkdtemp(prefix="kl_addr_")
        with open(os.path.join(d, "x.py"), "w", encoding="utf-8") as f:
            f.write('UA = "KL x (Someone@Example.test)"\n')
        self.assertEqual(holders("someone@example.test", ["x.py", "missing.py"], root=d), ["x.py"])


if __name__ == "__main__":
    unittest.main()
