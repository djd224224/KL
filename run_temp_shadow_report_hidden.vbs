' Hourly-temp shadow test's DAILY REPORT with NO console flash ("KL temp-shadow
' report", 08:30 ET daily; register_temp_shadow_report.ps1). One run of
' temp_shadow_report.py --send: sims the previous ET day (temp_shadow_sim),
' writes KL-data\temp-shadow\reports\<day>.html and emails it through
' send_html_email.py. stdout/stderr -- the run lines, a traceback -- append to
' KL-data\temp-shadow\report-task.log. The exit code is passed through so the
' task's Last Result shows a bad run (1 the sim failed or some events failed in
' it -- the report still went out; 2 the email failed).
' Added 2026-10-14 for the week-long KXTEMPMIAH shadow test (TEMP_SHADOW.md).
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& set PYTHONIOENCODING=utf-8&& """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"""" """"C:\Users\jackd\Documents\KL\temp_shadow_report.py"""" --send >> """"C:\Users\jackd\Documents\KL-data\temp-shadow\report-task.log"""" 2>&1""", 0, True)
