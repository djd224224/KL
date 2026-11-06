# Registers "KL combo-rfq logger" -- the always-on recorder of the combo maker
# shadow (combo_rfq_logger.py; Jack 2026-11-05: "Register the logger as an
# always-on Windows task so it captures every night"). READ-ONLY: it records a
# 2% server-side slice of Kalshi's combo RFQs and the public trade tape's combo
# prints; it never quotes or trades.
#
# Triggers: at logon (+1 min) AND every 10 min (repeating, indefinitely).
# IgnoreNew: while the restart loop runs the task is Running, so the 10-min
# trigger is a no-op; if the loop ever dies, the next trigger brings it back
# within 10 min. The logger's own combo_rfq_logger.lock refuses a second copy
# (exit 4, before any network call).
#
# ExecutionTimeLimit is UNLIMITED (PT0S): without it Task Scheduler applies a
# 72 h default and kills the wscript wrapper every 3 days, orphaning the loop
# (see fix_crypto_task_time_limit.ps1, 2026-10-11). Battery flags so a laptop
# on battery neither refuses to start it nor stops it. Interactive principal,
# like every KL task (runs while Jack is logged on).
#
# WINDOWLESS via run_combo_rfq_logger_hidden.vbs (wscript //B).
# Started once on registration. Safe to re-run (-Force replaces the
# definition; a running instance keeps running).
#
# Stop it: create KL\run-logs\combo-rfq\combo_rfq_logger.stop; for good, also
#   Unregister-ScheduledTask -TaskName 'KL combo-rfq logger' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL combo-rfq logger"
$Vbs = Join-Path $Repo "run_combo_rfq_logger_hidden.vbs"
$Data = Join-Path $Repo "run-logs\combo-rfq"
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
$desc = "Combo maker shadow recorder (2026-11-05, combo_rfq_logger.py): a 2% slice of Kalshi's " +
        "combo RFQs + the trade tape's combo prints -> KL\run-logs\combo-rfq. Read-only. " +
        "Stop: create run-logs\combo-rfq\combo_rfq_logger.stop; for good, Unregister-ScheduledTask " +
        "-TaskName 'KL combo-rfq logger' -Confirm:`$false"

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
Write-Host "Logs: $Data\launcher-<date>.log, $Data\combo_<UTC date>.jsonl"
