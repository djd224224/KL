# Registers "KL yt-rescore": the one-time YouTube markets re-score
# (run_yt_rescore.ps1), once at 11:00 AM ET Thu 2026-10-15 (converted to this
# box's clock). Replaces the paused Claude Desktop scheduled task
# "youtube-markets-rescore-oct15", whose runs stall on permission prompts
# (Jack 2026-10-12: "do the same for the youtube rescore on 10/15").
# 11:00, not 10:00: the "IMM bot improvements" session's cron runs the same
# re-score at 10:03 if that session is still open, and the launcher stands
# down when that run's output folder already exists.
# Wakes the PC, battery-safe, StartWhenAvailable so a trigger missed while
# off or asleep runs at the next chance, one instance at a time. 4 h limit:
# claude -p gets up to 3 h (a quick failure is retried once, 10 min later),
# plus the email.
# Safe to re-run: -Force replaces the definition.
#
# NOT started on registration. A plumbing test without email:
#   powershell -NoProfile -ExecutionPolicy Bypass -File run_yt_rescore.ps1 -DryRun -NoEmail
# Remove after it has run:
#   Unregister-ScheduledTask -TaskName 'KL yt-rescore' -Confirm:$false
$TaskName = "KL yt-rescore"
try {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $etWall = [datetime]::SpecifyKind([datetime]"2026-10-15 11:00", [DateTimeKind]::Unspecified)
    $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
} catch {
    $start = [datetime]"2026-10-15 11:00"
}
$settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName $TaskName `
    -Description "One-time IMM YouTube markets re-score (Jack, Oct 15): claude -p follows the paused desktop task's SKILL.md in KL-data\youtube-analysis-2026-10-15 and writes verdict.md, which is emailed; stands down if another run already made that folder (run_yt_rescore.ps1)." `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"C:\Users\jackd\Documents\KL\run_yt_rescore_hidden.vbs`"" `
        -WorkingDirectory "C:\Users\jackd\Documents\KL") `
    -Trigger (New-ScheduledTaskTrigger -Once -At $start) `
    -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: once at {0} local time." -f $start.ToString("ddd yyyy-MM-dd h:mm tt"))
