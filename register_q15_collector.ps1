# Registers "KL q15-shadow collector" -- the always-on recorder of the
# 15-minute shadow test (Q15_SHADOW.md; Jack 2026-10-22: "Yes use a live
# price feed like Miami temp, with recorder and a week of testing").
# READ-ONLY: it records Kalshi's 15-minute FX / yield / commodity / crypto-race
# books, trades, programs and settlements plus the underlyings' live prices;
# it never trades.
#
# Triggers: at logon (+1 min) AND every 10 min (repeating, indefinitely).
# IgnoreNew: while the restart loop runs the task is Running, so the 10-min
# trigger is a no-op; if the loop ever dies, the next trigger brings it back
# within 10 min. The collector's own collector.lock refuses a second copy.
#
# ExecutionTimeLimit is UNLIMITED (PT0S): without it Task Scheduler applies a
# 72 h default and kills the wscript wrapper every 3 days, orphaning the loop.
# Battery flags so a laptop on battery neither refuses to start it nor stops
# it. Interactive principal, like every KL task.
#
# WINDOWLESS via run_q15_collector_hidden.vbs (wscript //B).
# Started once on registration so recording begins now. Safe to re-run
# (-Force replaces the definition; a running instance keeps running).
#
# Stop the test: create KL-data\q15-shadow\HALT, then
#   Unregister-ScheduledTask -TaskName 'KL q15-shadow collector' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL q15-shadow collector"
$Vbs = Join-Path $Repo "run_q15_collector_hidden.vbs"
$Data = "C:\Users\jackd\Documents\KL-data\q15-shadow"
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
$desc = "15-minute shadow test recorder (2026-10-22, Q15_SHADOW.md): Kalshi 15-min FX / yields / " +
        "commodities / crypto-race WS books + trades, programs, settlements, and the underlyings' " +
        "live prices -> KL-data\q15-shadow. Read-only. Stop: create KL-data\q15-shadow\HALT, then " +
        "Unregister-ScheduledTask -TaskName 'KL q15-shadow collector' -Confirm:`$false"

Register-ScheduledTask -TaskName $TaskName -Force -Action $action -Trigger @($logon, $every10) `
    -Principal $principal -Settings $settings -Description $desc | Out-Null
Start-ScheduledTask -TaskName $TaskName

$t = Get-ScheduledTask -TaskName $TaskName
$t | Format-Table TaskName, State -AutoSize
Write-Host ("timeLimit={0} startOnBattery={1} stopOnBattery={2} multiple={3}" -f `
    $t.Settings.ExecutionTimeLimit, (-not $t.Settings.DisallowStartIfOnBatteries), `
    $t.Settings.StopIfGoingOnBatteries, $t.Settings.MultipleInstances)
Write-Host "Logs:   $Data\collector.log, $Data\launcher-<date>.log"
Write-Host "Status: $Data\collector_status.json"
