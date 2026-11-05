' Post-vote dead-book SCANNER with NO console flash (task "KL pvd-scan", every 15 min).
' Adds relit / new post-vote election programs to post_vote_dead_books.json by
' ROI, only where they project >= $1.30 by their program end; the bot hot-loads
' the list at its next universe refresh. Never places, cancels or removes.
' Added 2026-11-05 (Jack: "make sure to be ready to start quoting if these
' markets are relit overnight, or new similar markets are launched overnight").
' Remove: Unregister-ScheduledTask -TaskName 'KL pvd-scan' -Confirm:$false
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"""" """"C:\Users\jackd\Documents\KL\pvd_scan.py"""" --write >> """"C:\Users\jackd\Documents\KL\run-logs\pvd-scan\scan-task.log"""" 2>&1""", 0, True
