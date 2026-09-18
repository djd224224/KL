# Registers "KL imm scan-perf" — the daily open-scan performance scorer
# (imm_scan_perf.py), which re-derives what the scan tier actually traded and
# writes run-logs\incentive-mm\scan_perf.json for the bot to hot-reload.
# Safe to re-run: -Force replaces the definition.
#
# WHEN: daily 07:55 ET — five minutes after "KL imm program-history" (07:50)
# so reward_programs.json holds fresh period ends, and before the day's
# decisions matter.
#
# WHY THE TRIGGER IS PINNED HERE AND NOT DERIVED FROM A SIBLING TASK:
# register_imm_new_programs.ps1 reads a sibling task's own StartBoundary and
# adds 5 minutes. That pattern is why "KL imm new-programs" silently moved
# from 06:45 back to 07:30 on 2026-09-13 the moment its register script was
# re-run: the derived time overwrites a trigger a human moved by hand, and
# nothing in the output says a time was CHANGED rather than kept. A scoring
# job that feeds a live trading knob must not have a schedule that drifts as
# a side effect of re-running its own installer, so 07:55 ET is written
# literally below and converted into the box's LOCAL clock once, here.
# If the ET wall time ever needs to move, edit $EtHour/$EtMinute — never
# "inherit" it from another task.
#
# WHAT IT RUNS: cmd.exe wrapper (so >> redirection works), PYTHONPATH pointed
# at the user site-packages the other IMM tasks use, stdout+stderr appended to
# run-logs\incentive-mm\scan-perf-task.log. Interactive/Limited principal,
# StartWhenAvailable (a trigger missed in Modern Standby still runs),
# batteries allowed, PT2H execution limit (a cold run re-reads up to 45 days
# of cycle_log_*.csv at ~140 MB/day; the warm cached run is seconds).
#
# NOT started on registration, deliberately (the house convention): the first
# run writes the first scan_perf.json, and kicking it here would fire it at an
# arbitrary minute. Preview by hand instead, which writes NOTHING:
#   python imm_scan_perf.py --dry
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL imm scan-perf"
$Script = "imm_scan_perf.py"
$LogName = "scan-perf-task.log"
$EtHour = 7
$EtMinute = 55

# 1. The PINNED trigger: 07:55 ET expressed in the box's local clock.
try {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    # Kind must be Unspecified for ConvertTimeToUtc to read it as ET.
    $etWall = [datetime]::SpecifyKind((Get-Date).Date.AddHours($EtHour).AddMinutes($EtMinute),
                                      [DateTimeKind]::Unspecified)
    $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
} catch {
    $start = (Get-Date).Date.AddHours($EtHour).AddMinutes($EtMinute)
}

# 2. Command line. The Python path is taken from a sibling IMM task ONLY to
#    survive a Python upgrade (a hardcoded exe silently breaks the task: it
#    still "runs", the table just never appears); the SCHEDULE is never taken
#    from a sibling. The literal path is the fallback.
$exe = "cmd.exe"
$pyExe = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$pyPath = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
$principal = $null
foreach ($sib in @("KL imm program-history", "KL imm opportunistic", "KL imm quote-gaps")) {
    $t = Get-ScheduledTask -TaskName $sib -ErrorAction SilentlyContinue
    if (-not $t) { continue }
    $action = $t.Actions | Select-Object -First 1
    if ($action.Arguments -match '"([^"]*python\.exe)"') { $pyExe = $Matches[1] }
    if ($action.Arguments -match 'set PYTHONPATH=([^&]+)&') { $pyPath = $Matches[1].Trim() }
    $principal = $t.Principal
    break
}
$arg = '/c "set PYTHONPATH=' + $pyPath + '&& "' + $pyExe + '" ' +
       "`"$Repo\$Script`" --now >> `"$Repo\run-logs\incentive-mm\$LogName`" 2>&1`""

if (-not $principal) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited
}

New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\incentive-mm") | Out-Null

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action (New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $Repo) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $start) `
    -Principal $principal -Settings $settings | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: daily {0} local time (PINNED at {1}:{2:00} ET in this script)." -f
            $start.ToString("h:mm tt"), $EtHour, $EtMinute)
Write-Host "Preview without writing:  python $Repo\$Script --dry"
Write-Host "Kill switch: delete run-logs\incentive-mm\scan_perf.json (neutral within 10 min)."
