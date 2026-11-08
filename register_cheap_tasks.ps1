# Registers the two CHEAP-SIDE BOOKS tasks (Jack 2026-11-08: "ship and list at
# $50, give daily email report on performance. add a schedule"). Safe to re-run:
# -Force replaces each definition.
#
#   "KL cheap-scan"        hourly at :25 local -- run_cheap_scan_hidden.vbs ->
#                          cheap_scan.py --write (the truly-far series, $50 a market)
#   "KL imm cheap-report"  daily 08:30 local   -- run_cheap_report_hidden.vbs ->
#                          send_cheap_report.py (yesterday's email)
#
# Triggers are written as local wall time with no UTC offset (StartBoundary), so
# they follow DST: New-ScheduledTaskTrigger alone pins a UTC instant.
# Not started on registration. Preview by hand:
#   python cheap_scan.py --series KXDSENATESEATS,KXSTATE51,KXSCOTUSPOWER,KXFRENCHPRES
#   python send_cheap_report.py --dry
$Repo = "C:\Users\jackd\Documents\KL"
New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\cheap-scan") | Out-Null
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

# hourly re-scan: IgnoreNew so a slow read never stacks two runs
$scanSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$now = Get-Date
$first = $now.Date.AddHours($now.Hour).AddMinutes(25)
if ($first -le $now) { $first = $first.AddHours(1) }
$scanTrigger = New-ScheduledTaskTrigger -Once -At $first -RepetitionInterval (New-TimeSpan -Hours 1)
$scanTrigger.StartBoundary = $first.ToString('s')
Register-ScheduledTask -TaskName "KL cheap-scan" -Force `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"$Repo\run_cheap_scan_hidden.vbs`"" -WorkingDirectory $Repo) `
    -Trigger $scanTrigger -Principal $principal -Settings $scanSettings | Out-Null

# daily email: a 2h limit covers the cycle-log read and the 8x5min send retries
$mailSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$mail = $now.Date.AddHours(8).AddMinutes(30)
if ($mail -le $now) { $mail = $mail.AddDays(1) }
$mailTrigger = New-ScheduledTaskTrigger -Daily -At $mail
$mailTrigger.StartBoundary = $mail.ToString('s')
Register-ScheduledTask -TaskName "KL imm cheap-report" -Force `
    -Action (New-ScheduledTaskAction -Execute "wscript.exe" `
        -Argument "//B `"$Repo\run_cheap_report_hidden.vbs`"" -WorkingDirectory $Repo) `
    -Trigger $mailTrigger -Principal $principal -Settings $mailSettings | Out-Null

Get-ScheduledTask -TaskName "KL cheap-scan", "KL imm cheap-report" | Format-Table TaskName, State -AutoSize
Write-Host ("KL cheap-scan: hourly at :25 from {0}; KL imm cheap-report: daily 08:30 from {1}" -f
            $first.ToString("yyyy-MM-dd HH:mm"), $mail.ToString("yyyy-MM-dd"))
