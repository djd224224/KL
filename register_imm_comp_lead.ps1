# Registers "KL imm comp lead" -- the daily complement-lead and hedge email
# (send_comp_lead_monitor.py; Jack 2026-10-29: "I mean just a daily status
# email report on this mechanism"). Safe to re-run: -Force replaces the
# definition.
#
# WHEN: 35 minutes AFTER the 7:20 quote-gaps email (7:55 ET, after the 7:50
# lone-ref email), read off that sibling task's own trigger so the
# local-clock convention of the box is whatever the lineup already uses.
# With no sibling, 7:55 AM ET converted into local time.
#
# WHAT IT RUNS: the sibling's own cmd.exe line with the script and log names
# swapped (the Python path and PYTHONPATH stay whatever the box currently
# uses). Literal paths are the fallback.
#
# NOT started on registration. Preview by hand instead:
#   python send_comp_lead_monitor.py --dry
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL imm comp lead"
$Script = "send_comp_lead_monitor.py"
$LogName = "comp-lead-task.log"

$exe = $null; $arg = $null; $start = $null; $principal = $null; $from = ""
$t = Get-ScheduledTask -TaskName "KL imm quote-gaps" -ErrorAction SilentlyContinue
if ($t) {
    $action = $t.Actions | Select-Object -First 1
    $candidate = ""
    if ($action.Arguments) {
        $candidate = $action.Arguments.Replace("imm_quote_gaps.py", $Script).Replace(
            "quote-gaps-task.log", $LogName)
    }
    # Only trust the copy if BOTH swaps landed.
    if ($candidate -like "*$Script*" -and $candidate -like "*$LogName*") {
        $exe = $action.Execute
        $arg = $candidate
        $principal = $t.Principal
        $trigger = $t.Triggers | Select-Object -First 1
        if ($trigger.StartBoundary) {
            $start = ([datetime]$trigger.StartBoundary).AddMinutes(35)
        }
        $from = "KL imm quote-gaps"
    }
}
if (-not $arg) {
    $exe = "cmd.exe"
    $arg = '/c "set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& ' +
           '"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe" ' +
           "`"$Repo\$Script`" >> `"$Repo\run-logs\incentive-mm\$LogName`" 2>&1`""
    $from = "defaults (no quote-gaps task found)"
}
if (-not $start) {
    try {
        $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
        $etWall = [datetime]::SpecifyKind((Get-Date).Date.AddHours(7).AddMinutes(55),
                                          [DateTimeKind]::Unspecified)
        $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
    } catch {
        $start = (Get-Date).Date.AddHours(7).AddMinutes(55)
    }
}
if (-not $principal) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited
}

New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\incentive-mm") | Out-Null

# StartWhenAvailable so a trigger missed in standby still runs; batteries
# allowed and a 2h limit to cover the script's 8x5min send retries.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action (New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $Repo) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $start) `
    -Principal $principal -Settings $settings | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: daily {0} local time (schedule taken from {1})." -f
            $start.ToString("h:mm tt"), $from)
Write-Host "Preview without sending:  python $Repo\$Script --dry"
