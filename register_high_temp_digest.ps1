# Registers "KL high-temp-digest": the KXHIGH daily digest, 6:12 PM local
# daily (this PC runs on Eastern; the launcher works out the day in ET
# itself). Replaces the Claude Desktop scheduled task "high-temp-daily-digest",
# whose runs stalled on permission prompts (disabled 2026-10-12).
# Battery-safe, StartWhenAvailable for a trigger missed in standby, one
# instance at a time. 7 h limit: up to 5 h waiting for the evening BigQuery
# load, then up to 45 min of claude -p per day.
# Safe to re-run: -Force replaces the definition.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 7)
Register-ScheduledTask -TaskName "KL high-temp-digest" `
    -Description "KXHIGH daily digest: waits for the evening BigQuery load, claude -p writes run-logs\high-temp\<D>.html in its own worktree, then commit + push + email (run_high_temp_digest.ps1)." `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"C:\Users\jackd\Documents\KL\run_high_temp_digest_hidden.vbs`"" `
        -WorkingDirectory "C:\Users\jackd\Documents\KL") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 6:12PM) `
    -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName "KL high-temp-digest" |
    Format-Table TaskName, State -AutoSize
Write-Host "Registered: daily 6:12 PM local (ET)."
