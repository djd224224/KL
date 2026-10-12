' Runs run_high_temp_digest.ps1 with NO visible console window (it starts at
' 6:12 PM and can wait hours for the evening BigQuery load). Waits, so the
' task's execution time limit still applies, and passes the exit code on to
' Task Scheduler's Last Run Result.
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\jackd\Documents\KL\run_high_temp_digest.ps1""", 0, True)
