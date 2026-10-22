# Registers "KL q15-shadow report" -- the 15-minute shadow test's daily email
# (q15_report.py --send; Q15_SHADOW.md, verdict rules Q15_PREREG.md). Jack
# 2026-10-22: "Yes use a live price feed like Miami temp, with recorder and a
# week of testing". Safe to re-run: -Force replaces the definition.
#
# WHEN: daily 08:45 AM ET (Eastern Standard Time zone rules, so DST is
# handled; the box runs on Eastern), from 10/24 -- the first full verdict day
# is 10/23 -- to 10/30, whose run reports 10/29 and the week's verdict; the
# trigger expires at the EndBoundary 2026-10-31 12:00 local. The run sims the
# previous ET day (its last markets settle at 00:00 ET; the recorder pulls
# results within minutes).
#
# WHAT IT RUNS: wscript //B run_q15_report_hidden.vbs (windowless; output to
# KL-data\q15-shadow\report-task.log). StartWhenAvailable reruns a slot
# missed in standby; batteries allowed; a 2 h limit covers the day's sim.
#
# NOT started on registration. Preview by hand (no email):
#   python q15_report.py --day 2026-10-23
# Remove: Unregister-ScheduledTask -TaskName 'KL q15-shadow report' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL q15-shadow report"
$Vbs = Join-Path $Repo "run_q15_report_hidden.vbs"
$Log = "C:\Users\jackd\Documents\KL-data\q15-shadow\report-task.log"
if (-not (Test-Path $Vbs)) {
    Write-Host "Missing $Vbs"
    exit 1
}
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null

$first = [datetime]"2026-10-24"
try {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $etWall = [datetime]::SpecifyKind($first.AddHours(8).AddMinutes(45), [DateTimeKind]::Unspecified)
    $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
} catch {
    $start = $first.AddHours(8).AddMinutes(45)
}
$trigger = New-ScheduledTaskTrigger -Daily -At $start
$trigger.EndBoundary = ([datetime]"2026-10-31T12:00:00").ToString("s")
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B `"$Vbs`"" -WorkingDirectory $Repo
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$desc = "15-minute shadow test daily report (2026-10-22, Q15_SHADOW.md): sims the previous ET day " +
        "and emails variants, checks and the pre-registered verdict. Read-only; nothing trades."

Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Description $desc | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
$t | Format-Table TaskName, State -AutoSize
foreach ($tr in $t.Triggers) {
    Write-Host ("trigger start={0} end={1}" -f $tr.StartBoundary, $tr.EndBoundary)
}
Write-Host "Preview without sending:  python $Repo\q15_report.py --day <ET day>"
