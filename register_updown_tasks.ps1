# Registers + starts the 7 crypto_updown_mm daily+weekly-tenor tasks.
# (Weekly-only until 2026-08-13; the daily tenor — the Mon-Thu 5pm-ET KX*D
# events, listed ~25h ahead — was added per Jack "build bot for annual and
# daily crypto markets". Fridays the 5pm close IS the weekly event, so no
# separate daily lists that day.)
# NEEDS ELEVATION — run via register_updown_tasks.bat (self-elevates) or from
# an admin PowerShell.
#
# REFUSES to run while any updown task is Running: re-registering a live task
# (-Force) detaches its running launcher chain, which keeps trading outside
# task control — that minted a duplicate updown fleet on 2026-08-09. Stop the
# tasks (and kill their chains) first, or use fix_updown_battery.ps1 if you
# only need to change task settings.
$assets = "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"

$alive = @(Get-ScheduledTask -TaskName "KL crypto_updown_mm *" -ErrorAction SilentlyContinue |
           Where-Object State -eq "Running")
if ($alive.Count -gt 0) {
    Write-Host "ABORTING: $($alive.Count) updown task(s) are Running. Re-registering live"
    Write-Host "tasks orphans their launcher chains (duplicate bots outside task control)."
    Write-Host "Stop them first, or use fix_updown_battery.ps1 for settings-only changes."
    exit 1
}

# Battery-safe like every other KL task: start on battery, survive AC unplug.
# (The 2026-08-08 registration used defaults, so one unplug stopped the fleet.)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach ($a in $assets) {
    Register-ScheduledTask -TaskName "KL crypto_updown_mm $a" `
        -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
            -Argument "//B `"C:\Users\jackd\Documents\KL\run_crypto_updown_mm_hidden.vbs`" $a daily,weekly 30") `
        -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName "KL crypto_updown_mm $a"
    Write-Host "registered + started KL crypto_updown_mm $a"
}
Write-Host ""
Get-ScheduledTask -TaskName "KL crypto_updown_mm *" | Format-Table TaskName, State -AutoSize
Write-Host "Done - all 7 should show Running (launchers add 0-45s jitter before the bots quote)."
