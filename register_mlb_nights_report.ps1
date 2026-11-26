# Registers "KL imm mlb-nights": the MLB next team nights pilot's morning
# email (Jack 2026-11-26: "give email daily report"). Daily at 07:15 local
# (this PC runs on Eastern), a quarter hour after each 00:00-07:00 ET quote
# window closes and before the 7:25 opportunistic email, from 11/27 to 12/03
# -- the 12/03 run is the final one, after the programs' 12/03 05:00Z end.
# The trigger expires after that, and the script itself sends nothing outside
# the pilot. Wakes the PC, battery-safe, StartWhenAvailable so a trigger
# missed in standby runs at the next chance (the day's marker keeps it to
# one email), one instance at a time, 20 min limit.
# Safe to re-run: -Force replaces the definition. Remove after the pilot:
#   Unregister-ScheduledTask -TaskName 'KL imm mlb-nights' -Confirm:$false
$TaskName = "KL imm mlb-nights"
$settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]"2026-11-27 07:15")
$trigger.EndBoundary = ([datetime]"2026-12-03 23:59").ToString("s")
Register-ScheduledTask -TaskName $TaskName `
    -Description "MLB next team nights pilot (KXMLBNEXTTEAM, 00:00-07:00 ET, x3, to 12/03): the morning email of the night's fills, modelled rewards, trading at last trade, positions, price moves and stops (send_mlb_nights_report.py via run_mlb_nights_report.ps1). Output and task.log in run-logs\incentive-mm\mlb_nights\." `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"C:\Users\jackd\Documents\KL\run_mlb_nights_report_hidden.vbs`"" `
        -WorkingDirectory "C:\Users\jackd\Documents\KL") `
    -Trigger $trigger -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host "Registered: daily 7:15 AM local (ET), 2026-11-27 through 2026-12-03."
