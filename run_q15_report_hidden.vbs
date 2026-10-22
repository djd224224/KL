' 15-minute shadow test daily report with NO console flash ("KL q15-shadow
' report", daily 08:45 ET; register_q15_report.ps1). Sims the previous ET day
' and emails the report (q15_report.py --send); output appended to
' KL-data\q15-shadow\report-task.log; the exit code passed through.
' Added 2026-10-22 (Q15_SHADOW.md).
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& set PYTHONIOENCODING=utf-8&& ""C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"" ""C:\Users\jackd\Documents\KL\q15_report.py"" --send >> ""C:\Users\jackd\Documents\KL-data\q15-shadow\report-task.log"" 2>&1""", 0, True)
