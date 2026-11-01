# Registers "KL imm rt-settled" -- the daily RT settled-strikes email
# (send_rt_settled_monitor.py; Jack 2026-11-01: "run it live, and then email
# report on performnce so i can monitor it"). Safe to re-run: -Force replaces
# the definition.
#
# WHEN: 08:05 local (the box runs on Eastern), ten minutes after the 07:55
# leads email. The trigger's StartBoundary is written as local wall time with
# no UTC offset, so it follows DST: New-ScheduledTaskTrigger alone pins a UTC
# instant, which moved every morning email an hour on 11/01.
#
# WHAT IT RUNS: the leads email task's own cmd.exe line with the script and
# log names swapped (the Python path and PYTHONPATH stay whatever the box
# uses). Literal paths are the fallback.
#
# NOT started on registration. Preview by hand instead:
#   python send_rt_settled_monitor.py --dry
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL imm rt-settled"
$Script = "send_rt_settled_monitor.py"
$LogName = "rt-settled-task.log"
$Sibling = "KL imm comp lead"
$SiblingScript = "send_comp_lead_monitor.py"
$SiblingLog = "comp-lead-task.log"

$exe = $null; $arg = $null; $principal = $null; $from = ""
$t = Get-ScheduledTask -TaskName $Sibling -ErrorAction SilentlyContinue
if ($t) {
    $action = $t.Actions | Select-Object -First 1
    $candidate = ""
    if ($action.Arguments) {
        $candidate = $action.Arguments.Replace($SiblingScript, $Script).Replace($SiblingLog, $LogName)
    }
    # Only trust the copy if BOTH swaps landed.
    if ($candidate -like "*$Script*" -and $candidate -like "*$LogName*") {
        $exe = $action.Execute
        $arg = $candidate
        $principal = $t.Principal
        $from = $Sibling
    }
}
if (-not $arg) {
    $exe = "cmd.exe"
    $arg = '/c "set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& ' +
           '"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe" ' +
           "`"$Repo\$Script`" >> `"$Repo\run-logs\incentive-mm\$LogName`" 2>&1`""
    $from = "defaults (no leads email task found)"
}
if (-not $principal) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited
}

New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\incentive-mm") | Out-Null

# StartWhenAvailable so a trigger missed in standby still runs; batteries
# allowed and a 2h limit to cover the day-log read and the script's 8x5min
# send retries.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# local wall time, no offset (follows DST); the first run is the next 08:05
$first = (Get-Date).Date.AddHours(8).AddMinutes(5)
if ($first -le (Get-Date)) { $first = $first.AddDays(1) }
$trigger = New-ScheduledTaskTrigger -Daily -At $first
$trigger.StartBoundary = $first.ToString('s')

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action (New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $Repo) `
    -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: daily 08:05 local time from {0} (command taken from {1})." -f
            $first.ToString("yyyy-MM-dd"), $from)
Write-Host "Preview without sending:  python $Repo\$Script --dry"
