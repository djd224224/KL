# Registers "KL remote-control" — the always-on Remote Control host that makes
# this box reachable from the Claude mobile app. Safe to re-run: -Force
# replaces the definition.
#
# WHEN: at logon, not on a clock. Remote Control authenticates as the logged-in
# user, so there is no useful moment before that; and the host is meant to be
# up whenever the box is up.
#
# THE SETTINGS ARE THE INTERESTING PART — a persistent server breaks every
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
#
# NOT started on registration when the CLI is signed out, deliberately: the
# launcher would just alert and exit, burning an email. The script tells you.
$Repo = "C:\Users\jackd\Documents\KL"
$TaskName = "KL remote-control"
$Launcher = Join-Path $Repo "run_remote_control.ps1"
$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"

if (-not (Test-Path $Launcher)) { throw "Launcher not found: $Launcher" }

New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\remote-control") | Out-Null

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Launcher`"" `
    -WorkingDirectory $Repo

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Limited

# -ExecutionTimeLimit 0 == PT0S == run forever.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Force `
    -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null

Get-ScheduledTask -TaskName $TaskName | Format-Table TaskName, State -AutoSize

# Only offer to start it if the CLI can actually authenticate. `claude auth
# status` exits 0 even when signed out, so read the JSON, not the exit code.
$loggedIn = $false
try { $loggedIn = [bool]((& $Claude auth status 2>&1 | Out-String | ConvertFrom-Json).loggedIn) } catch {}

if ($loggedIn) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Registered and started. The box should appear as 'trading-box' in the"
    Write-Host "Claude mobile app's Code tab (and at claude.ai/code) within a few seconds."
} else {
    Write-Host ""
    Write-Host "Registered, but NOT started: the claude CLI is signed out." -ForegroundColor Yellow
    Write-Host "Sign in on the box, then start it:"
    Write-Host "    claude auth login"
    Write-Host "    Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host ""
    Write-Host "The same sign-in also unblocks the 5:45 AM 'KL kalshi-daily-recap' task."
}

# Registration itself succeeded either way; the signed-out path is a notice,
# not a failure, and a stray $LASTEXITCODE from the auth probe must not say so.
exit 0
