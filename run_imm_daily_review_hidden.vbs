' Runs run_imm_daily_review.ps1 (the IMM daily review, 04:30 ET) with NO
' visible console window. Waits, so the task's execution time limit still
' applies, and passes the exit code on to Task Scheduler's Last Run Result
' (0 = review sent, 1 = email failed, 2 = the evidence-only fallback went out).
Dim sh
Set sh = CreateObject("WScript.Shell")
WScript.Quit sh.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -File ""C:\Users\jackd\Documents\KL\run_imm_daily_review.ps1""", 0, True)
