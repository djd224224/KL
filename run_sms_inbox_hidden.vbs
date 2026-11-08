' TEXT REPLIES -> CLAUDE with NO console flash (task "KL sms-inbox", every minute).
' Reads Gmail for texts from Jack's number (T-Mobile's email gateway), checks
' T-Mobile's DKIM + SPF, runs a read-only Claude on each new one and texts the
' answer back; a text may only STOP things (phone_actions.py). Added 2026-11-08
' (Jack: "can i set it up so if i text back and it goes to Claude?", then
' "Yes, build it"). Log: run-logs\sms_inbox.log (+ sms-inbox-task.log for crashes).
' Remove: Unregister-ScheduledTask -TaskName 'KL sms-inbox' -Confirm:$false
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"""" """"C:\Users\jackd\Documents\KL\sms_inbox.py"""" >> """"C:\Users\jackd\Documents\KL\run-logs\sms-inbox-task.log"""" 2>&1""", 0, True
