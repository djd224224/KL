' Launches send_daily_digest.py with NO visible console window (the 7am
' digest can run 40+ minutes through its network-retry loop; without this
' a console window sits on screen the whole time). Waits so the task's
' execution time limit still applies.
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c """"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"" ""C:\Users\jackd\Documents\KL\send_daily_digest.py"" >> ""C:\Users\jackd\Documents\KL\run-logs\crypto-touch\digest-task.log"" 2>&1""", 0, True
