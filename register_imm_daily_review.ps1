# Registers "KL imm daily-review": the IMM daily review (Jack 2026-11-25),
# 04:30 local daily (this PC runs on Eastern). The launcher reviews the prior
# ET day: imm_review.py collect -> claude -p with the imm-daily-review skill ->
# send_imm_review.py, so the email lands around 06:00-06:30, before the 07:00
# cluster. Battery-safe and wakes the PC; StartWhenAvailable catches a trigger
# missed in standby; one instance at a time. 2 h 45 min limit: collect up to
# 25 min (the very first run back-fills 14 days of log signatures, ~5 min),
# claude up to 100 min with one retry, the send with retries.
# Safe to re-run: -Force replaces the definition.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2 -Minutes 45)
Register-ScheduledTask -TaskName "KL imm daily-review" `
    -Description "IMM daily review: evidence pack (imm_review.py), claude -p imm-daily-review skill, email (send_imm_review.py). Ledger + outputs in run-logs\incentive-mm\daily_review\. (run_imm_daily_review.ps1)" `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"C:\Users\jackd\Documents\KL\run_imm_daily_review_hidden.vbs`"" `
        -WorkingDirectory "C:\Users\jackd\Documents\KL") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 4:30AM) `
    -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName "KL imm daily-review" |
    Format-Table TaskName, State -AutoSize
Write-Host "Registered: daily 4:30 AM local (ET)."
