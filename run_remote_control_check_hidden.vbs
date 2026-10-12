' Runs remote_control_check.ps1 (the hourly Remote Control check) with NO
' console flash, waiting on it. Task "KL remote-control check".
' Usage: wscript.exe //B run_remote_control_check_hidden.vbs
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & _
      """C:\Users\jackd\Documents\KL\remote_control_check.ps1"""
WScript.Quit sh.Run(cmd, 0, True)
