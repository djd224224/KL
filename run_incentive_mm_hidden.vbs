' Launches run_incentive_mm.ps1 -Probe with NO visible console window.
' Task Scheduler runs an interactive task in a visible console, and that
' window WAS the bot: closing it on 2026-09-28 22:35Z (Jack took it for a
' stray PowerShell) killed launcher and bot outright -- no shutdown_cancel
' ran, so ~660 orders rested unmanaged until the watchdog relaunched the task
' five minutes later. Same wrapper as the crypto fleets
' (run_crypto_annual_mm_hidden.vbs): windowstyle 0, and it WAITS on the
' launcher, so the task stays "Running" exactly as long as the launcher lives
' (watchdog_imm_guard.ps1 reads "Ready" as the launcher having died).
' Ending the task is not guaranteed to take the launcher under this wscript
' with it (Stop-ScheduledTask already orphaned the python child on
' 2026-08-01), and an orphaned launcher keeps relaunching python -- so
' restart_imm.ps1 -Task sweeps the whole chain before it starts the task.
' Usage: wscript.exe //B run_incentive_mm_hidden.vbs
Dim sh, cmd
Set sh = CreateObject("WScript.Shell")
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File " & _
      """C:\Users\jackd\Documents\KL\run_incentive_mm.ps1"" -Probe"
WScript.Quit sh.Run(cmd, 0, True)
