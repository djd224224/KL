# Registers "KL remote-control" -- the always-on Remote Control host that makes
# this box reachable from the Claude mobile app -- and "KL remote-control
# check", its hourly check. Safe to re-run: -Force replaces both definitions,
# and a running host is restarted under the new one (a ~30 s gap).
#
# WHEN: at logon AND every 5 minutes (2026-10-12). Remote Control
# authenticates as the logged-in user, so there is no useful moment before
# logon. Logon alone was the 9/26 failure: the launcher died that noon and
# nothing started it again for 16 days. The 5-minute trigger with IgnoreNew is
# a no-op while the launcher runs and a restart within 5 minutes when it
# doesn't -- including after a dead login is fixed.
#
# THE SETTINGS ARE THE INTERESTING PART -- a persistent server breaks every
# default Task Scheduler assumption:
#   ExecutionTimeLimit 0  - the default kills a task after 3 days (and the KL
#                           tasks have been bitten by a PT20M limit before).
#                           A front door that dies on a timer is worse than no
#                           front door, because nothing reports it.
#   IgnoreNew             - one host, always. A second host racing the first
#                           would register a duplicate machine on claude.ai.
#   Batteries allowed     - this is a laptop that lives on Modern Standby; the
#                           bot fleets already run under the same allowances.
#   RestartCount          - belt and braces. run_remote_control.ps1 supervises
#                           itself, so this only catches the launcher dying.
#   wscript wrapper       - run_remote_control_hidden.vbs: no console window to
#                           close by mistake (how it died on 9/26), and it
#                           waits, so "Running" means the launcher is alive.
#
# NOT started on registration when the login is dead, deliberately: the
# launcher would just alert and exit. The script tells you.
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL remote-control"
$CheckTaskName = "KL remote-control check"
$SessionName = "trading-box"
$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
$Wrapper = Join-Path $Repo "run_remote_control_hidden.vbs"
$CheckWrapper = Join-Path $Repo "run_remote_control_check_hidden.vbs"

foreach ($f in $Wrapper, $CheckWrapper, (Join-Path $Repo "run_remote_control.ps1"),
               (Join-Path $Repo "remote_control_check.ps1"), (Join-Path $Repo "remote_control_common.ps1")) {
    if (-not (Test-Path $f)) { throw "Not found: $f" }
}

$LogDir = Join-Path $Repo "run-logs\remote-control"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("{0:yyyy-MM-dd}.log" -f (Get-Date))
. (Join-Path $Repo "remote_control_common.ps1")

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

# --- the host ----------------------------------------------------------------
$action = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument "//B `"$Wrapper`"" -WorkingDirectory $Repo

# No -RepetitionDuration = repeat indefinitely (checked after registering).
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 5))
)

# -ExecutionTimeLimit 0 == PT0S == run forever.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

# --- the hourly check ----------------------------------------------------------
$checkAction = New-ScheduledTaskAction -Execute "wscript.exe" `
    -Argument "//B `"$CheckWrapper`"" -WorkingDirectory $Repo
$checkTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(7) `
    -RepetitionInterval (New-TimeSpan -Hours 1)
$checkSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -MultipleInstances IgnoreNew

# A launcher already running (perhaps the old, windowed one) is stopped with
# its whole chain and restarted under the new definition below.
$wasRunning = ((Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue).State -eq 'Running') -or
              (@(Get-HostProcesses $SessionName).Count -gt 0)
if ($wasRunning) {
    Write-Log "register: stopping the running host chain to move it under the new definition"
    Stop-LauncherChain $TaskName $SessionName
}

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action $action -Trigger $triggers -Principal $principal -Settings $settings | Out-Null
Register-ScheduledTask -TaskName $CheckTaskName -Force `
    -Action $checkAction -Trigger $checkTrigger -Principal $principal -Settings $checkSettings | Out-Null

foreach ($n in $TaskName, $CheckTaskName) {
    $t = Get-ScheduledTask -TaskName $n
    $reps = ($t.Triggers | ForEach-Object {
        if ($_.Repetition -and $_.Repetition.Interval) {
            "every $($_.Repetition.Interval) for $(if ($_.Repetition.Duration) { $_.Repetition.Duration } else { 'ever' })"
        } else { $_.CimClass.CimClassName -replace 'MSFT_Task', '' -replace 'Trigger', '' }
    }) -join '; '
    Write-Host ("{0}: {1}  [{2}]" -f $n, $t.State, $reps)
}

# Only start it if the login actually works -- a real request, not `claude auth
# status` (which said loggedIn=true all through the 10/12 expiry).
$probe = Test-ClaudeLogin
if ($probe.auth) {
    Write-Host ""
    Write-Host "Registered, but NOT started: the claude login is dead ($($probe.why))." -ForegroundColor Yellow
    Write-Host "Sign in on the box; the 5-minute trigger then starts it by itself:"
    Write-Host "    claude auth login"
} else {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Registered and started. The box should appear as 'trading-box' in the"
    Write-Host "Claude mobile app's Code tab (and at claude.ai/code) within a few seconds."
    if (-not $probe.ok) { Write-Host "(login probe inconclusive: $($probe.why))" }
}

# Registration itself succeeded either way; the dead-login path is a notice,
# not a failure, and a stray $LASTEXITCODE from the probe must not say so.
exit 0
