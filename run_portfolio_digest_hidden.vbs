' Launches send_portfolio_digest.py with NO visible console window (the 7am-ET
' portfolio email can run 40+ minutes through its network-retry loop; without
' this a console window sits on screen the whole time). PYTHONPATH points at
' the user site-packages (matplotlib lives there; Task Scheduler's stripped
' env doesn't resolve it on its own — same fix as the quote-gaps task).
' Waits so the task's execution time limit still applies.
Dim sh
Set sh = CreateObject("WScript.Shell")
sh.Run "cmd.exe /c ""set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& ""C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"" ""C:\Users\jackd\Documents\KL\send_portfolio_digest.py"" >> ""C:\Users\jackd\Documents\KL\run-logs\portfolio-digest\digest-task.log"" 2>&1""", 0, True
