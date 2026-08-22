# Registers + starts the 8 crypto_annual_mm tasks (one per asset).
# Run via register_annual_tasks.bat (self-elevates) or from an admin
# PowerShell; plain non-admin PowerShell usually works too for current-user
# at-logon tasks — the .bat wrapper exists for parity with the updown fleet.
#
# REFUSES to run while any annual task is Running: re-registering a live task
# (-Force) detaches its running launcher chain, which keeps trading outside
# task control — that minted a duplicate updown fleet on 2026-08-09. Stop the
# tasks (and kill their chains) first.
$assets = "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE", "ZEC"

$alive = @(Get-ScheduledTask -TaskName "KL crypto_annual_mm *" -ErrorAction SilentlyContinue |
           Where-Object State -eq "Running")
if ($alive.Count -gt 0) {
    Write-Host "ABORTING: $($alive.Count) annual task(s) are Running. Re-registering live"
    Write-Host "tasks orphans their launcher chains (duplicate bots outside task control)."
    Write-Host "Stop them first (Stop-ScheduledTask + kill the wscript/powershell/cmd/python chains)."
    exit 1
}

# On a RE-registration, disable any existing (Ready) tasks before replacing
# them: the 15-min watchdog starts Ready 'KL crypto_annual_mm *' tasks, and a
# tick landing between the guard above and Register -Force below would revive
# a task that -Force then orphans (the 2026-08-09 updown mechanism). Disabled
# tasks are skipped by the watchdog; Register -Force replaces them enabled.
Get-ScheduledTask -TaskName "KL crypto_annual_mm *" -ErrorAction SilentlyContinue |
    Disable-ScheduledTask -ErrorAction SilentlyContinue | Out-Null

# Battery-safe like every other KL task: start on battery, survive AC unplug.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach ($a in $assets) {
    Register-ScheduledTask -TaskName "KL crypto_annual_mm $a" `
        -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument "//B `"C:\Users\jackd\Documents\KL\run_crypto_annual_mm_hidden.vbs`" $a 60") `
        -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName "KL crypto_annual_mm $a"
    Write-Host "registered + started KL crypto_annual_mm $a"
}
Write-Host ""
Get-ScheduledTask -TaskName "KL crypto_annual_mm *" | Format-Table TaskName, State -AutoSize
Write-Host "Done - all 8 should show Running (launchers add 0-45s jitter before the bots quote)."
