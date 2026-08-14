# Makes the 7 crypto_updown_mm tasks battery-safe, matching every other KL
# task: allow starting on battery, don't stop when AC is unplugged.
# NEEDS ELEVATION - run via fix_updown_battery.bat (self-elevates).
#
# Background (2026-08-09): the tasks were registered with Task Scheduler
# DEFAULTS (DisallowStartIfOnBatteries + StopIfGoingOnBatteries), so a brief
# AC unplug stopped the whole updown fleet while every other bot kept running.
# Set-ScheduledTask only updates the task definition - it does NOT restart the
# running instance, so this is safe to run while the bots are live (no
# duplicate/orphan chains are created).
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
foreach ($a in "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE") {
    Set-ScheduledTask -TaskName "KL crypto_updown_mm $a" -Settings $settings | Out-Null
    Write-Host "patched KL crypto_updown_mm $a"
}
Write-Host ""
Get-ScheduledTask -TaskName "KL crypto_updown_mm *" | ForEach-Object {
    '{0,-26} {1,-8} startOnBattery={2} stopOnBattery={3}' -f $_.TaskName, $_.State,
        (-not $_.Settings.DisallowStartIfOnBatteries), $_.Settings.StopIfGoingOnBatteries
}
Write-Host ""
Write-Host "Done - all 7 should show startOnBattery=True stopOnBattery=False."
