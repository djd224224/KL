# Registers "KL imm gas-trial" -- the 1pm-midnight gas trial's 1-week and
# 2-week reports (imm_gas_trial_report.py; Jack 2026-10-01: "adjust all three
# to trade 1pm-midnight. and report on results at the 1 week and 2 week
# marks"). Safe to re-run: -Force replaces the definition.
#
# WHEN: daily, 25 minutes AFTER the 7:20 quote-gaps email (7:45 ET), read off
# that sibling task's own trigger so the box's local-clock convention is
# whatever the lineup already uses (fallback: 7:45 AM ET converted to local).
# The script itself decides whether a report is due -- it emails only on the
# first run at or after day 7 and day 14 of the trial and exits quietly on
# every other day -- so the task can run daily whatever day the trial began.
#
# WHAT IT RUNS: the sibling's own cmd.exe line with the script and log names
# swapped (Python path and PYTHONPATH stay whatever the box uses). Literal
# paths are the fallback.
#
# NOT started on registration. Preview by hand instead:
#   python imm_gas_trial_report.py --dry
# Remove after the 2-week report:
#   Unregister-ScheduledTask -TaskName 'KL imm gas-trial' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL imm gas-trial"
$Script = "imm_gas_trial_report.py"
$LogName = "gas-trial-task.log"

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
            $start = ([datetime]$trigger.StartBoundary).AddMinutes(25)
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
        $etWall = [datetime]::SpecifyKind((Get-Date).Date.AddHours(7).AddMinutes(45),
                                          [DateTimeKind]::Unspecified)
        $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
    } catch {
        $start = (Get-Date).Date.AddHours(7).AddMinutes(45)
    }
}
if (-not $principal) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited
}

New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\incentive-mm") | Out-Null

# StartWhenAvailable so a trigger missed in standby still runs; batteries
# allowed and a 1h limit to cover the script's 6x5min send retries.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action (New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $Repo) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $start) `
    -Principal $principal -Settings $settings | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: daily {0} local time (schedule taken from {1})." -f
            $start.ToString("h:mm tt"), $from)
Write-Host "Preview without sending:  python $Repo\$Script --dry"
