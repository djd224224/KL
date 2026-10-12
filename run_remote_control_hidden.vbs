' Runs run_remote_control.ps1 (the Remote Control host launcher) with NO
' console window, and WAITS on it, so the "KL remote-control" task is
' "Running" exactly as long as the launcher lives and the task's 5-minute
' trigger (MultipleInstances IgnoreNew) only ever starts a launcher that is gone.
' Why (Jack 2026-10-12, "ensure that im never in a position where i cant
' access"): the launcher ran in a visible console, died 2026-09-26 ~noon with
' 0xC000013A (window closed), and stayed down 16 days -- the same way the IMM
' bot's window got closed on 9/28 (run_incentive_mm_hidden.vbs).
' Usage: wscript.exe //B run_remote_control_hidden.vbs
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & _
      """C:\Users\jackd\Documents\KL\run_remote_control.ps1"""
WScript.Quit sh.Run(cmd, 0, True)
