' Combo maker shadow's read-only RFQ logger with NO console flash ("KL combo-rfq
' logger", at logon + every 10 min with IgnoreNew; register_combo_rfq_logger.ps1).
' Runs the run_combo_rfq_logger.ps1 restart loop and WAITS for it, so the task
' stays Running while the loop lives and IgnoreNew keeps it single.
' The loop logs to KL\run-logs\combo-rfq\launcher-<date>.log; the logger writes
' combo_<UTC date>.jsonl beside it. Exit code passed through (3 = the stop
' file, 4 = another logger holds the lock).
' Added 2026-11-05 (Jack: "Register the logger as an always-on Windows task").
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Users\jackd\Documents\KL\run_combo_rfq_logger.ps1""", 0, True)
