' Runs run_mlb_nights_report.ps1 (the MLB nights pilot's morning email,
' 07:15 ET) with NO visible console window. Passes the task's arguments on,
' waits, so the task's execution time limit still applies, and hands the exit
' code to Task Scheduler's Last Run Result (0 ok, 1 the send failed).
Dim sh, i, a
Set sh = CreateObject("WScript.Shell")
a = ""
For i = 0 To WScript.Arguments.Count - 1
    a = a & " " & WScript.Arguments(i)
Next
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\jackd\Documents\KL\run_mlb_nights_report.ps1""" & a, 0, True)
