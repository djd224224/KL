' The daily CHEAP-SIDE BOOKS email with NO console flash (task "KL imm cheap-report", 08:30).
' send_cheap_report.py: yesterday's modelled rewards vs fills on the cheap-side books, the
' running total since launch, KEEP / WATCH / STOP. Added 2026-11-08 (Jack: "give daily
' email report on performance").
' First cheap_scan.py --discover (~5 min): the program series OUTSIDE the list where a 1-2c
' rung would pay, for the email's "new far-dated series for you to approve" section (Jack
' 2026-11-08: "Add it"). It lists nothing; the email runs whether or not it succeeds.
' Remove: Unregister-ScheduledTask -TaskName 'KL imm cheap-report' -Confirm:$false
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"""" """"C:\Users\jackd\Documents\KL\cheap_scan.py"""" --discover --max-usd 50 >> """"C:\Users\jackd\Documents\KL\run-logs\cheap-scan\discover-task.log"""" 2>&1""", 0, True
sh.Run "cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"""" """"C:\Users\jackd\Documents\KL\send_cheap_report.py"""" >> """"C:\Users\jackd\Documents\KL\run-logs\incentive-mm\cheap-report-task.log"""" 2>&1""", 0, True
