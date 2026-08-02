' Hidden launcher for sync_kl_main.ps1 — window style 0 so Task Scheduler
' doesn't flash a console every 30 minutes. Waits for PowerShell to finish
' (async fire-and-forget got reaped when the task completed) and passes the
' exit code through to Task Scheduler.
rc = CreateObject("Wscript.Shell").Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\jackd\Documents\KL\sync_kl_main.ps1""", 0, True)
WScript.Quit rc
