# Registers "KL imm log-retention": the IMM log retention (review R-261124-03,
# Jack 2026-11-25: "Yes, 35 days"). It runs daily at 02:40 local (this PC runs
# on Eastern): in the bot's quiet hours, before the 04:30 daily review reports
# it. The task's action carries the job's arguments:
#   (default)   --apply --not-before 2026-11-27 --max-gb 10
#               dry runs until 11/27 (the readers get a full day live first),
#               then up to 10 GB of logs a night until the backlog is done
#   -DryRun     no --apply: every run only lists what it would compress
# Battery-safe; StartWhenAvailable catches a trigger missed in standby; one
# instance at a time. 2 h limit: the job starts no new file after 90 minutes.
# Safe to re-run: -Force replaces the definition.
param(
    [switch]$DryRun,
    [string]$NotBefore = "2026-11-27",
    [double]$MaxGb = 10
)
$jobArgs = if ($DryRun) { "--max-gb $MaxGb" } else { "--apply --not-before $NotBefore --max-gb $MaxGb" }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "KL imm log-retention" `
    -Description "IMM log retention (review R-261124-03): gzips the bot's text logs older than 35 days in place, verified byte for byte; nothing is deleted. Manifest + last_run.json in run-logs\incentive-mm\retention\. Stop: IMM_LOG_RETENTION=0 or a retention\STOP file. (run_imm_log_retention.ps1)" `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"C:\Users\jackd\Documents\KL\run_imm_log_retention_hidden.vbs`" $jobArgs" `
        -WorkingDirectory "C:\Users\jackd\Documents\KL") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 2:40AM) `
    -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName "KL imm log-retention" |
    Format-Table TaskName, State -AutoSize
Write-Host "Registered: daily 2:40 AM local (ET): imm_log_retention.py $jobArgs"
