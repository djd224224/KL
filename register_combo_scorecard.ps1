# Registers "KL combo scorecard" -- the combo desk's whole-tape scorecard, emailed
# every morning (combo_scorecard.py --email; Jack 2026-11-06: "yes i want the
# scorecard every morning"). It scores yesterday's UTC day of the RFQ logger's
# prints (7 PM ET to 7 PM ET), so the evening's games have settled by 7:30 AM.
# Safe to re-run: -Force replaces the definition.
#
# WHEN: 7:30 AM by the LOCAL clock (this box runs on ET). The trigger is written
# with no UTC offset, so it follows daylight saving -- New-ScheduledTaskTrigger
# alone pins a UTC instant, which fired every morning email an hour early when
# DST ended (11/01). Dated tomorrow: StartWhenAvailable would run a boundary
# already past today at once.
#
# NOT started on registration. Preview by hand instead:
#   python combo_scorecard.py --day yesterday            # the files, no email
#   python combo_scorecard.py --email --test             # send now, no marker
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL combo scorecard"
$Log = "$Repo\run-logs\combo-rfq\scorecard-task.log"
$Py = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"

New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null
$arg = '/c "set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& ' +
       "`"$Py`" `"$Repo\combo_scorecard.py`" --email >> `"$Log`" 2>&1`""

$trigger = New-ScheduledTaskTrigger -Daily -At (Get-Date).Date.AddDays(1).AddHours(7).AddMinutes(30)
$trigger.StartBoundary = (Get-Date).Date.AddDays(1).ToString("yyyy-MM-dd") + "T07:30:00"   # local, no offset

# StartWhenAvailable so a morning missed in standby still runs; batteries allowed;
# 2 h covers a slow day (the maker's 300 MB log) and the 6 x 5-min send retries.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action (New-ScheduledTaskAction -Execute "cmd.exe" -Argument $arg -WorkingDirectory $Repo) `
    -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
Write-Host ("Registered {0}: {1}, daily from {2} (local time, follows DST)" -f
            $TaskName, $t.State, ($t.Triggers | Select-Object -First 1).StartBoundary)
Write-Host "Log: $Log"
