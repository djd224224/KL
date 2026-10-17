' Runs run_risk_dry_report.ps1 (the one-time 3-day report on the IMM's two
' log-only risk rules, Task Scheduler "KL risk-dry-report") with NO visible
' console window. Passes its arguments on (-NoEmail for a test). Waits, so the
' task's execution time limit still applies, and passes the exit code on to
' Task Scheduler's Last Run Result.
Dim sh, args, i
Set sh = CreateObject("WScript.Shell")
args = ""
For i = 0 To WScript.Arguments.Count - 1
    args = args & " " & WScript.Arguments(i)
Next
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\jackd\Documents\KL\run_risk_dry_report.ps1""" & args, 0, True)
