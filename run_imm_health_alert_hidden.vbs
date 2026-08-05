' IMM liveness alerter with NO console flash (runs every 5 min).
' READ-ONLY: emails on a health transition, restarts nothing. Recovery is
' run_watchdog_hidden.vbs's job (every 15 min, Ready-only) — noticing and
' repairing are different guarantees and 2026-08-05 showed you need both.
' Added 2026-08-05 (Jack: "if watcher fires, send me an email alert").
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"""" """"C:\Users\jackd\Documents\KL\imm_health_alert.py"""" >> """"C:\Users\jackd\Documents\KL\run-logs\incentive-mm\health-alert.log"""" 2>&1""", 0, True
