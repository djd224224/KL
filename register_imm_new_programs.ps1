# Registers "KL imm new-programs" — the daily "new incentive programs" email
# (send_imm_new_programs.py), the last of the IMM morning lineup. Safe to
# re-run: -Force replaces the definition, and nothing else on the box points
# at this task.
#
# WHEN: right after the existing lineup, derived from the machine rather than
# hardcoded. The other IMM email tasks are registered in the box's LOCAL clock
# and the handoff notes label them in ET, so a literal "7:30AM" here would be
# a guess about which convention is true on this machine. Instead: read the
# sibling task's own trigger and add 5 minutes (10 after quote-gaps), which is
# right under either convention. With no sibling registered, fall back to
# 7:30 AM ET converted into local time.
#
# WHAT IT RUNS: the sibling's own cmd.exe line with the script and log names
# swapped, so the Python path and PYTHONPATH stay whatever the box currently
# uses (a Python upgrade silently breaks a hardcoded path — the task still
# "runs", the email just never arrives). Literal paths are the fallback.
#
# NOT started on registration, deliberately: the first run of this email is a
# seeding run, and kicking it here would fire it at an arbitrary minute. Send
# a preview by hand instead:
#   python send_imm_new_programs.py --dry
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL imm new-programs"
$Script = "send_imm_new_programs.py"
$LogName = "new-programs-task.log"

# 1. Trigger time + command line, from a sibling IMM email task when present.
$siblings = @(
    @{ Task = "KL imm opportunistic"; Py = "send_opportunistic_imm.py"
       Log = "opportunistic-task.log"; Offset = 5 },
    @{ Task = "KL imm quote-gaps";    Py = "imm_quote_gaps.py"
       Log = "quote-gaps-task.log";   Offset = 10 }
)
$exe = $null; $arg = $null; $start = $null; $principal = $null; $from = ""
foreach ($s in $siblings) {
    $t = Get-ScheduledTask -TaskName $s.Task -ErrorAction SilentlyContinue
    if (-not $t) { continue }
    $action = $t.Actions | Select-Object -First 1
    $candidate = ""
    if ($action.Arguments) {
        $candidate = $action.Arguments.Replace($s.Py, $Script).Replace($s.Log, $LogName)
    }
    # Only trust the copy if BOTH swaps actually landed — a sibling whose
    # command line has been rewritten by hand must not hand us its own script.
    if ($candidate -like "*$Script*" -and $candidate -like "*$LogName*") {
        $exe = $action.Execute
        $arg = $candidate
        $principal = $t.Principal
        $trigger = $t.Triggers | Select-Object -First 1
        if ($trigger.StartBoundary) {
            $start = ([datetime]$trigger.StartBoundary).AddMinutes($s.Offset)
        }
        $from = $s.Task
        break
    }
}

# 2. Fallbacks: the documented command line, and 7:30 AM ET in local time.
if (-not $arg) {
    $exe = "cmd.exe"
    $arg = '/c "set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& ' +
           '"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe" ' +
           "`"$Repo\$Script`" >> `"$Repo\run-logs\incentive-mm\$LogName`" 2>&1`""
    $from = "defaults (no sibling IMM email task found)"
}
if (-not $start) {
    try {
        $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
        # Kind must be Unspecified for ConvertTimeToUtc to read it as ET.
        $etWall = [datetime]::SpecifyKind((Get-Date).Date.AddHours(7).AddMinutes(30),
                                          [DateTimeKind]::Unspecified)
        $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
    } catch {
        $start = (Get-Date).Date.AddHours(7).AddMinutes(30)
    }
}
if (-not $principal) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited
}

New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\incentive-mm") | Out-Null

# StartWhenAvailable so a trigger missed in standby still runs; batteries
# allowed and a 2h limit to cover the script's 8x5min Modern-Standby retries.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action (New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $Repo) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $start) `
    -Principal $principal -Settings $settings | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: daily {0} local time (schedule taken from {1})." -f
            $start.ToString("h:mm tt"), $from)
try {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $etNow = [TimeZoneInfo]::ConvertTime([datetimeoffset]$start, $et)
    Write-Host ("That is {0} ET." -f $etNow.ToString("h:mm tt"))
} catch { }
Write-Host "Preview without sending:  python $Repo\$Script --dry"
