# Registers "KL cpi-pilot-report": the one-time review of the IMM CPI core
# pilot (run_cpi_pilot_report.ps1), once at 8:00 AM ET Tue 2026-10-13
# (converted to this box's clock). Replaces the Claude Desktop scheduled task
# "cpi-core-pilot-report", whose run would have stalled on a permission
# prompt (disabled 2026-10-12, Jack: Windows job over a hand run).
# Wakes the PC, battery-safe, StartWhenAvailable so a trigger missed while
# off or asleep runs at the next chance, one instance at a time. 2 h limit:
# two claude -p attempts of up to 40 min, 10 min apart, plus the email.
# Safe to re-run: -Force replaces the definition.
#
# NOT started on registration. A test without email:
#   powershell -NoProfile -ExecutionPolicy Bypass -File run_cpi_pilot_report.ps1 -NoEmail
# Remove after it has sent:
#   Unregister-ScheduledTask -TaskName 'KL cpi-pilot-report' -Confirm:$false
$TaskName = "KL cpi-pilot-report"
try {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $etWall = [datetime]::SpecifyKind([datetime]"2026-10-13 08:00", [DateTimeKind]::Unspecified)
    $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
} catch {
    $start = [datetime]"2026-10-13 08:00"
}
$settings = New-ScheduledTaskSettingsSet -WakeToRun -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName $TaskName `
    -Description "One-time IMM CPI core pilot review (Jack, Oct 13): claude -p runs imm_cpi_pilot_report.py from its own worktree and writes the keep/block review, which is emailed (run_cpi_pilot_report.ps1). Replaces the desktop task cpi-core-pilot-report." `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"C:\Users\jackd\Documents\KL\run_cpi_pilot_report_hidden.vbs`"" `
        -WorkingDirectory "C:\Users\jackd\Documents\KL") `
    -Trigger (New-ScheduledTaskTrigger -Once -At $start) `
    -Settings $settings -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: once at {0} local time." -f $start.ToString("ddd yyyy-MM-dd h:mm tt"))
