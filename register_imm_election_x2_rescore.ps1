# Registers "KL imm election-x2-rescore" -- the one-time 10/18 re-score of
# the elections family x2 (imm_election_x2_rescore.py; Jack 2026-10-11: "yes
# double elections, re-score 10/18 and email me"). Safe to re-run: -Force
# replaces the definition.
#
# WHEN: once, Sun 2026-10-18 08:10 ET (converted to the box's local clock).
# StartWhenAvailable, so a trigger missed in standby runs at the next wake;
# the script itself refuses to send before 08:00 ET 10/18 and sends only once
# (election_x2_rescore_sent.marker).
#
# WHAT IT RUNS: the "KL imm quote-gaps" task's own cmd.exe line with the
# script and log names swapped (Python path and PYTHONPATH stay whatever the
# box uses). Literal paths are the fallback.
#
# NOT started on registration. Preview by hand instead:
#   python imm_election_x2_rescore.py --dry
# Remove after it has sent:
#   Unregister-ScheduledTask -TaskName 'KL imm election-x2-rescore' -Confirm:$false
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL imm election-x2-rescore"
$Script = "imm_election_x2_rescore.py"
$LogName = "election-x2-rescore-task.log"

$exe = $null; $arg = $null; $principal = $null; $from = ""
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
try {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $etWall = [datetime]::SpecifyKind([datetime]"2026-10-18 08:10", [DateTimeKind]::Unspecified)
    $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
} catch {
    $start = [datetime]"2026-10-18 08:10"
}
if (-not $principal) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited
}

# batteries allowed and a 1h limit to cover the script's 6x5min send retries
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action (New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $Repo) `
    -Trigger (New-ScheduledTaskTrigger -Once -At $start) `
    -Principal $principal -Settings $settings | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: once at {0} local time (command taken from {1})." -f
            $start.ToString("ddd yyyy-MM-dd h:mm tt"), $from)
Write-Host "Preview without sending:  python $Repo\$Script --dry"
