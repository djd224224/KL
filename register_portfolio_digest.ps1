# Registers the daily whole-account portfolio email task: 6:00 AM local
# (Central) = 7:00 AM ET, matching the crypto DIGEST / imm quote-gaps
# conventions (battery-safe, StartWhenAvailable for missed triggers after
# standby, 2h execution limit to cover the Modern-Standby retry loops).
# Safe to re-run: -Force replaces the definition (one-shot task, no
# launcher chain to orphan).
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName "KL portfolio DIGEST" `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"C:\Users\jackd\Documents\KL\run_portfolio_digest_hidden.vbs`"" `
        -WorkingDirectory "C:\Users\jackd\Documents\KL") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 6:00AM) `
    -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName "KL portfolio DIGEST" |
    Format-Table TaskName, State -AutoSize
Write-Host "Registered: daily 6:00 AM local (7:00 AM ET)."
