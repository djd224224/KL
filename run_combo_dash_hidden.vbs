' The combo maker's read-only dashboard (http://127.0.0.1:8772) with NO console
' flash ("KL combo-dash", at logon + every 10 min with IgnoreNew;
' register_combo_dash.ps1). Runs the run_combo_dash.ps1 restart loop and WAITS
' for it, so the task stays Running while the loop lives and IgnoreNew keeps it
' single. The loop logs to KL\run-logs\combo-dash\launcher-<date>.log. Exit code
' passed through (3 = the stop file, 4 = port 8772 is taken).
' Added 2026-11-06 (Jack: "Yes, add the task").
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\jackd\Documents\KL\run_combo_dash.ps1""", 0, True)
