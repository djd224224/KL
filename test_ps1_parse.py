"""Every PowerShell script at the repo root parses.

Found on 11/26 while building the moat backup (R-261124-20): the line
    Send-Alert "IMM review $Day: moat backup FAILED" ...
is a PowerShell PARSE error ("$Day:" reads as a scope-qualified variable), and
a parse error stops a script before its first line runs: the whole 04:30 review
launcher would have died with no email and no alert. No test caught it; this
one checks all of them with PowerShell's own parser (nothing is executed)."""

import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PS = ("$errs = @(); Get-ChildItem -Path '{root}' -Filter *.ps1 -File | ForEach-Object { "
      "$t = $null; $e = $null; "
      "[void][System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$t, [ref]$e); "
      "if ($e.Count) { $errs += ($_.Name + ':' + $e[0].Extent.StartLineNumber + ': ' + $e[0].Message) } }; "
      "$errs; 'checked'")


class Ps1ParseTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("powershell") or shutil.which("pwsh"), "no PowerShell on this machine")
    def test_every_ps1_parses(self):
        exe = shutil.which("powershell") or shutil.which("pwsh")
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", PS.replace("{root}", HERE)],
                             capture_output=True, text=True, timeout=180)
        lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
        self.assertTrue(lines and lines[-1] == "checked", out.stdout + out.stderr)
        self.assertEqual(lines[:-1], [], "PowerShell parse errors (the script would not run at all)")


if __name__ == "__main__":
    unittest.main()
