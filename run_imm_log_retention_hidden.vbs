' Runs run_imm_log_retention.ps1 (the IMM log retention, 02:40 ET; review
' R-261124-03) with NO visible console window. Passes the task's arguments on
' (e.g. --apply --not-before 2026-11-27 --max-gb 10), waits, so the task's
' execution time limit still applies, and hands the exit code to Task
' Scheduler's Last Run Result (0 ok, 1 a file failed, 2 a .gz conflict,
' 3 refused, 4 another run holds the lock).
Dim sh, i, a
Set sh = CreateObject("WScript.Shell")
a = ""
For i = 0 To WScript.Arguments.Count - 1
    a = a & " " & WScript.Arguments(i)
Next
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\jackd\Documents\KL\run_imm_log_retention.ps1""" & a, 0, True)
