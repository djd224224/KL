# Registers "KL temp-shadow report" -- the hourly-temp shadow test's daily
# report (temp_shadow_report.py --send; TEMP_SHADOW.md "Daily report").
# Jack 2026-10-14: "build it and shadow test for a week, reporting results and
# suggestion tweaks daily". Safe to re-run: -Force replaces the definition.
#
# WHEN: daily 08:30 AM ET, converted into local time the way
# register_imm_carbon_arc.ps1 converts its 7:35 ET (Eastern Standard Time zone
# rules, so DST is handled; the box itself runs on Eastern). The run sims the
# previous ET day, whose last event settles at 00:00 ET and is pulled by the
# collector by ~02:00 ET at the latest. The trigger expires at the
# EndBoundary, 2026-10-24 12:00 local: the last run (10/24 08:30) reports the
# partial final day (the collector stops at 2026-10-23 12:00Z = 08:00 ET, so
# only events settling before then are due) and the cumulative week; day 7
# (10/21, reported 10/22) carries the first week summary. The verdict counts
# full days only: the partial 10/23 is shown beside it, weighted 8/24 in the
# per-day means, never averaged in as a whole day.
#
# WHAT IT RUNS: wscript //B run_temp_shadow_report_hidden.vbs (windowless; the
# VBS appends output to KL-data\temp-shadow\report-task.log and passes the exit
# code through). Interactive, like every KL task (runs while Jack is logged
# on). StartWhenAvailable reruns a slot missed in standby; batteries allowed;
# a 2 h limit covers the day's sim (24 events x 16 variant passes) plus the
# email retries.
#
# NOT started on registration. Preview by hand instead (no email):
#   python temp_shadow_report.py --no-sim --day 2026-10-15
# Remove: Unregister-ScheduledTask -TaskName 'KL temp-shadow report' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL temp-shadow report"
$Vbs = Join-Path $Repo "run_temp_shadow_report_hidden.vbs"
$Log = "C:\Users\jackd\Documents\KL-data\temp-shadow\report-task.log"
if (-not (Test-Path $Vbs)) {
    Write-Host "Missing $Vbs"
    exit 1
}
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null

try {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $etWall = [datetime]::SpecifyKind((Get-Date).Date.AddHours(8).AddMinutes(30),
                                      [DateTimeKind]::Unspecified)
    $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
} catch {
    $start = (Get-Date).Date.AddHours(8).AddMinutes(30)
}
# today's 08:30 already gone -> first run tomorrow (no catch-up run tonight)
if ($start -le (Get-Date)) { $start = $start.AddDays(1) }

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B `"$Vbs`"" `
    -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Daily -At $start
$trigger.EndBoundary = "2026-10-24T12:00:00"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$desc = "Hourly-temp (KXTEMPMIAH) shadow test daily report (2026-10-14): sims the previous " +
        "ET day and emails reward / trading / net per policy variant plus suggested tweaks. " +
        "Never trades. Remove: Unregister-ScheduledTask -TaskName 'KL temp-shadow report' -Confirm:`$false"

Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description $desc | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: daily {0} local time (08:30 ET), until 2026-10-24 12:00." -f
            $start.ToString("h:mm tt"))
Write-Host "Log:     $Log"
Write-Host "Preview: python $Repo\temp_shadow_report.py --no-sim"
