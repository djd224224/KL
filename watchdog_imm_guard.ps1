# Recovery guard for 'KL incentive_mm', called by run_watchdog_hidden.vbs.
#
# WHY THIS EXISTS. The naive rule -- "task is Ready, so start it" -- is unsafe
# for THIS task, because run_incentive_mm.ps1 is a blocking supervisor: it
# waits on python and relaunches it forever. So a dead bot does NOT leave the
# task Ready; the launcher just restarts it. Ready means the LAUNCHER died.
#
# And when the launcher dies its python child can survive, orphaned and still
# trading. That is not hypothetical: 2026-08-06 12:41:59Z had task=Ready with
# pid 32644 alive and quoting. The watchdog was 12 minutes from starting a
# SECOND bot onto the same live account -- two instances placing, cancelling
# and colliding on the same markets. Caught by hand with minutes to spare.
#
# So: start ONLY when the task is Ready *and* no incentive_mm python exists.
# The orphan case is deliberately left alone rather than auto-killed -- taking
# down a live book unattended is a bigger call than a human should have made
# for them. imm_health_alert.py already reports it ("task Ready while pid
# alive -- launcher exited") so it gets noticed and decided.

# TaskName is a parameter ONLY so the refuse-to-start branch can be exercised
# against a throwaway task; production callers pass nothing.
param([string]$TaskName = 'KL incentive_mm')

$ErrorActionPreference = 'SilentlyContinue'
$LogPath  = 'C:\Users\jackd\Documents\KL\run-logs\incentive-mm\watchdog-imm.log'

function Write-WdLog([string]$Message) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ssZ')
    Add-Content -Path $LogPath -Value "$stamp $Message"
}

$task = Get-ScheduledTask -TaskName $TaskName
if (-not $task) { Write-WdLog "! task '$TaskName' not found"; exit 1 }
if ($task.State -ne 'Ready') { exit 0 }          # Running/Disabled = nothing to do

# Disabled stays the deliberate pause switch; State -ne 'Ready' already covers it.
$alive = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
           Where-Object { $_.CommandLine -like '*incentive_mm.py*' })

if ($alive.Count -gt 0) {
    # -f binds tighter than +, so formatting a CONCATENATED literal silently
    # leaves {0}/{1} unexpanded in the first half. Build the string first.
    $pids = ($alive.ProcessId) -join ','
    Write-WdLog ("ORPHAN: task Ready but $($alive.Count) incentive_mm process(es) " +
                 "alive (pid $pids); NOT starting -- would duplicate the bot " +
                 "on a live account")
    exit 0
}

Write-WdLog "task Ready and no bot process alive; starting '$TaskName'"
Start-ScheduledTask -TaskName $TaskName
