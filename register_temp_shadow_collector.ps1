# Registers "KL temp-shadow collector" -- the always-on recorder of the
# hourly-temp shadow test (TEMP_SHADOW.md; Jack 2026-10-14: "build it and
# shadow test for a week"). READ-ONLY: it records Kalshi's KXTEMPMIAH books,
# trades, station feed and reward programs; it never trades.
#
# Triggers: at logon (+1 min) AND every 10 min (repeating, indefinitely).
# IgnoreNew: while the restart loop runs the task is Running, so the 10-min
# trigger is a no-op; if the loop ever dies, the next trigger brings it back
# within 10 min. The collector's own collector.lock refuses a second copy.
#
# ExecutionTimeLimit is UNLIMITED (PT0S): without it Task Scheduler applies a
# 72 h default and kills the wscript wrapper every 3 days, orphaning the loop
# (see fix_crypto_task_time_limit.ps1, 2026-10-11). Battery flags so a laptop
# on battery neither refuses to start it nor stops it. Interactive principal,
# like every KL task (runs while Jack is logged on).
#
# WINDOWLESS via run_temp_shadow_collector_hidden.vbs (wscript //B).
# Started once on registration so recording begins now. Safe to re-run
# (-Force replaces the definition; a running instance keeps running).
#
# Stop the test: create KL-data\temp-shadow\HALT, then
#   Unregister-ScheduledTask -TaskName 'KL temp-shadow collector' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL temp-shadow collector"
$Vbs = Join-Path $Repo "run_temp_shadow_collector_hidden.vbs"
$Data = "C:\Users\jackd\Documents\KL-data\temp-shadow"
if (-not (Test-Path $Vbs)) {
    Write-Host "Missing $Vbs"
    exit 1
}

$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "//B `"$Vbs`"" -WorkingDirectory $Repo
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$logon.Delay = "PT1M"
$now = Get-Date
$every10 = New-ScheduledTaskTrigger -Once -At $now.AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 10)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$desc = "Hourly-temp shadow test recorder (2026-10-14, TEMP_SHADOW.md): KXTEMPMIAH WS books + " +
        "trades, Miami station feed, programs, settlements -> KL-data\temp-shadow. Read-only. " +
        "Stop: create KL-data\temp-shadow\HALT, then Unregister-ScheduledTask -TaskName " +
        "'KL temp-shadow collector' -Confirm:`$false"

Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Trigger @($logon, $every10) `
    -Principal $principal -Settings $settings -Description $desc | Out-Null
Start-ScheduledTask -TaskName $TaskName

$t = Get-ScheduledTask -TaskName $TaskName
$t | Format-Table TaskName, State -AutoSize
Write-Host ("timeLimit={0} startOnBattery={1} stopOnBattery={2} multiple={3}" -f `
    $t.Settings.ExecutionTimeLimit, (-not $t.Settings.DisallowStartIfOnBatteries), `
    $t.Settings.StopIfGoingOnBatteries, $t.Settings.MultipleInstances)
foreach ($tr in $t.Triggers) {
    Write-Host ("trigger {0} start={1} delay={2} every={3} for={4}" -f $tr.CimClass.CimClassName, `
        $tr.StartBoundary, $tr.Delay, $tr.Repetition.Interval, $tr.Repetition.Duration)
}
Write-Host "Logs:   $Data\collector.log, $Data\launcher-<date>.log"
Write-Host "Status: $Data\collector_status.json"
