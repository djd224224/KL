' Launches run_crypto_touch_mm.ps1 with NO visible console window.
' Task Scheduler runs interactive tasks with a visible conhost; wrapping the
' launcher in wscript (windowstyle 0) hides it. The script WAITS on the
' launcher so the scheduled task stays "Running" (the watchdog task treats
' "Ready" as dead-and-restartable — a non-waiting wrapper would make it
' spawn duplicate fleets).
' Usage: wscript.exe //B run_crypto_touch_mm_hidden.vbs <MARKET> <POLLSECS>
Dim sh, market, poll, cmd
Set sh = CreateObject("WScript.Shell")
market = WScript.Arguments(0)
poll = WScript.Arguments(1)
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & _
      """C:\Users\jackd\Documents\KL\run_crypto_touch_mm.ps1""" & _
      " -Market """ & market & """ -PollSecs " & poll
sh.Run cmd, 0, True
