# Registers the daily 7:00 AM dashboard rebuild ('KL dashboards-daily').
# Idempotent: -Force re-registers cleanly. Run once from a normal PowerShell:
#   powershell -ExecutionPolicy Bypass -File register_dashboard_task.ps1
# (If Register-ScheduledTask is access-denied, rerun from an admin PowerShell.)
# The 'KL ' name prefix keeps it inside migrate_export.ps1's task-export glob.
Register-ScheduledTask -TaskName "KL dashboards-daily" `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"C:\Users\jackd\Documents\KL\run_dashboards_hidden.vbs`"") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 7:00AM) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)) `
    -Force | Out-Null
Write-Host "registered 'KL dashboards-daily': daily 7:00 AM, wakes the PC, catches up if the 7 AM slot was missed"
Get-ScheduledTask -TaskName "KL dashboards-daily" | Format-Table TaskName, State -AutoSize
