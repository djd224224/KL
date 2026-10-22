' 15-minute shadow test recorder with NO console flash ("KL q15-shadow
' collector", at logon + every 10 min with IgnoreNew; register_q15_collector.ps1).
' Runs the run_q15_collector.ps1 restart loop and WAITS for it, so the task
' stays Running while the loop lives and IgnoreNew keeps it single.
' The loop logs to KL-data\q15-shadow\launcher-<date>.log; the collector to
' KL-data\q15-shadow\collector.log. Exit code passed through (3 = past
' Q15_SHADOW_UNTIL or HALT, 4 = another collector holds the lock).
' Added 2026-10-22 (Q15_SHADOW.md).
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\jackd\Documents\KL\run_q15_collector.ps1""", 0, True)
