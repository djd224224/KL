' Launches run_dashboards.ps1 with NO visible console window (same pattern as
' run_digest_hidden.vbs). Waits so the task's execution time limit applies.
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\jackd\Documents\KL\run_dashboards.ps1""", 0, True
