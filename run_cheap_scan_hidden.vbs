' CHEAP-SIDE BOOKS re-scan with NO console flash (task "KL cheap-scan", hourly at :25).
' Rewrites run-logs\incentive-mm\cheap_side_books.json for the truly-far series below
' (outcome known 6M+ out): one 1-2c rung on the cheap side of every market that pays,
' at most $100 a market ($50 until Jack 2026-11-08 "raise to $100"); the bot hot-loads it at its next universe refresh. A market it
' cannot read keeps its entry; one listed for its program period keeps it to the end.
' Added 2026-11-08 (Jack: "ship and list at $50, give daily email report on performance.
' add a schedule"; the series: "Truly far: Dem Senate seats 2029, 51st state, Supreme
' Court makeup 2029, French presidency 2027 ^these all fit", plus the 2028 popular-vote /
' electoral-college margins, Senate seats 2029, next Moon landing, 2030 energy source).
' Remove: Unregister-ScheduledTask -TaskName 'KL cheap-scan' -Confirm:$false
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"""" """"C:\Users\jackd\Documents\KL\cheap_scan.py"""" --series KXDSENATESEATS,KXSTATE51,KXSCOTUSPOWER,KXFRENCHPRES,POPVOTEMOV,KXSENATESEATSD,ECMOV,KXMOONMAN,KXPRIMEENGCONSUMPTION --max-usd 100 --write >> """"C:\Users\jackd\Documents\KL\run-logs\cheap-scan\scan-task.log"""" 2>&1""", 0, True
