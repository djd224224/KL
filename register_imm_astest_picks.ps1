# Registers "KL imm astest-picks" -- the AS test's daily picks
# (imm_astest_picks.py; Jack 2026-10-27: "pick 5 distinct events each day"),
# which writes run-logs\incentive-mm\astest_picks.json for the live bot to
# read at its next universe refresh. Safe to re-run: -Force replaces the
# definition.
#
# WHEN: 10:30 AM ET daily, after the morning listings (the AAA gas dailies
# list at 08:00 ET, VoteHub-style dailies near 10:00 ET) and the 10/26 gas
# test's own ranking hour. Converted to the box's local clock.
#
# WHAT IT RUNS: the "KL imm opportunistic" task's own cmd.exe line with the
# script and log names swapped, so the Python path and PYTHONPATH stay
# whatever the box uses; literal paths are the fallback.
#
# NOT started on registration. Run one by hand instead:
#   python imm_astest_picks.py --dry-run --verbose
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL imm astest-picks"
$Script = "imm_astest_picks.py"
$LogName = "astest-picks-task.log"

$exe = $null; $arg = $null; $principal = $null; $from = ""
$t = Get-ScheduledTask -TaskName "KL imm opportunistic" -ErrorAction SilentlyContinue
if ($t) {
    $action = $t.Actions | Select-Object -First 1
    if ($action.Arguments) {
        $candidate = $action.Arguments.Replace("send_opportunistic_imm.py", $Script).Replace(
            "opportunistic-task.log", $LogName)
        if ($candidate -like "*$Script*" -and $candidate -like "*$LogName*") {
            $exe = $action.Execute
            $arg = $candidate
            $principal = $t.Principal
            $from = "KL imm opportunistic"
        }
    }
}
if (-not $arg) {
    $exe = "cmd.exe"
    $arg = '/c "set PYTHONPATH=C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages&& ' +
           '"C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe" ' +
           "`"$Repo\$Script`" >> `"$Repo\run-logs\incentive-mm\$LogName`" 2>&1`""
    $from = "defaults (no KL imm opportunistic task found)"
}
if (-not $principal) {
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
        -LogonType Interactive -RunLevel Limited
}
try {
    $et = [TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
    $etWall = [datetime]::SpecifyKind((Get-Date).Date.AddHours(10).AddMinutes(30),
                                      [DateTimeKind]::Unspecified)
    $start = [TimeZoneInfo]::ConvertTimeToUtc($etWall, $et).ToLocalTime()
} catch {
    $start = (Get-Date).Date.AddHours(10).AddMinutes(30)
}

# StartWhenAvailable so a trigger missed in standby still runs; a 1h limit
# (a first run reads ~25 series' tapes at ~2 requests/s; later runs reuse
# the cache).
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action (New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $Repo) `
    -Trigger (New-ScheduledTaskTrigger -Daily -At $start) `
    -Principal $principal -Settings $settings | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize
Write-Host ("Registered: daily {0} local time (command line from {1})." -f
            $start.ToString("h:mm tt"), $from)
