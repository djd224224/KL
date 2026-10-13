# Hourly check behind the Remote Control host ("KL remote-control" task,
# run_remote_control.ps1), run by the "KL remote-control check" task through
# run_remote_control_check_hidden.vbs.
#
# Jack 2026-10-12: "i do want to use remote control when i dont have access to
# PC. ensure that im never in a position where i cant access". The host had
# been down since 9/26 with nothing saying so. The host task now restarts
# itself every 5 minutes; this is the second line, and the one that notices a
# dead login while the host is still "up" (a host that outlives its login
# stalls without exiting, and `claude auth status` keeps saying loggedIn):
#   1. the host task not Running -> start it;
#   2. Running, but no host process on two checks in a row -> restart the task
#      (one miss can be the launcher's restart backoff, up to 5 minutes);
#   3. one real request on the /login the host runs on (Test-ClaudeLogin):
#      dead -> email at once, then every 6h; working again -> one all-clear.
#
# Logs to run-logs\remote-control\check-YYYY-MM-DD.log.
#   powershell -NoProfile -ExecutionPolicy Bypass -File remote_control_check.ps1

$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$SessionName = "trading-box"
$TaskName = "KL remote-control"

Set-Location $Repo
$LogDir = Join-Path $Repo "run-logs\remote-control"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("check-{0:yyyy-MM-dd}.log" -f (Get-Date))

. (Join-Path $Repo "remote_control_common.ps1")

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Log "task '$TaskName' is MISSING"
    Send-AlertThrottled 'missing' 24 "remote-control task MISSING" `
        ("The scheduled task '$TaskName' is gone, so nothing runs the Remote Control " +
         "host and the box is NOT reachable from mobile. Re-create it on the PC:`n`n" +
         "    powershell -NoProfile -ExecutionPolicy Bypass -File $Repo\register_remote_control.ps1")
    exit 1
}
Clear-Alert 'missing' $null $null
if ($task.State -eq 'Disabled') {
    # someone switched it off on purpose; say so in the log only
    Write-Log "task is Disabled; leaving it off"
    exit 0
}

$missFile = Join-Path $LogDir "check_host_missing.txt"
$hosts = @(Get-HostProcesses $SessionName)
if ($task.State -ne 'Running') {
    Write-Log "task was $($task.State); starting it"
    Start-ScheduledTask -TaskName $TaskName
    Remove-Item $missFile -ErrorAction SilentlyContinue
} elseif ($hosts.Count -eq 0) {
    if (Test-Path $missFile) {
        Write-Log "task Running but no host process on two checks; restarting the task"
        Stop-LauncherChain $TaskName $SessionName
        Start-Sleep -Seconds 5
        Start-ScheduledTask -TaskName $TaskName
        Remove-Item $missFile -ErrorAction SilentlyContinue
    } else {
        Write-Log "task Running but no host process (backoff?); checking again next hour"
        (Get-Date).ToUniversalTime().ToString('o') | Set-Content -Path $missFile -Encoding ascii
    }
} else {
    Write-Log ("host up: pid " + (($hosts | ForEach-Object { $_.ProcessId }) -join ','))
    Remove-Item $missFile -ErrorAction SilentlyContinue
}

# THE BACKUP PATH (Jack 2026-10-12): Chrome Remote Desktop. Its host service
# ("chromoting") starts at boot, so it reaches the PC even at the Windows
# sign-in screen after a restart (this org-joined PC can't auto sign in), and
# whatever state the claude login is in. Starting a service needs admin, which
# this task doesn't have, so a stopped one is an email.
$crd = Get-Service -Name 'chromoting' -ErrorAction SilentlyContinue
if (-not $crd) {
    Write-Log "Chrome Remote Desktop host: not installed"
} elseif ($crd.Status -ne 'Running') {
    Write-Log "Chrome Remote Desktop host: $($crd.Status)"
    Send-AlertThrottled 'crd' 6 "Chrome Remote Desktop host NOT running" `
        ("The Chrome Remote Desktop host service on the trading box is $($crd.Status), " +
         "so the backup way in (it works even at the Windows sign-in screen) is down.`n`n" +
         "Fix on the PC, in an admin PowerShell:`n    Start-Service chromoting`n" +
         "or open remotedesktop.google.com/access and turn remote access back on.`n" +
         "Log: $LogPath")
} else {
    Write-Log "Chrome Remote Desktop host: running"
    Clear-Alert 'crd' "Chrome Remote Desktop host running again" `
        "The Chrome Remote Desktop host service on the trading box is running again. Log: $LogPath"
}

$probe = Test-ClaudeLogin
if ($probe.auth) {
    Write-Log "login DEAD: $($probe.why)"
    Send-AlertThrottled 'login' 6 "remote-control DOWN: claude login expired" `
        ($LoginDeadBody + "`n`nLog: $LogPath")
    exit 1
}
if ($probe.ok) {
    Write-Log "login ok"
    Clear-Alert 'login' "remote-control: claude login OK again" `
        "The claude login on the trading box works again; the Remote Control host 'trading-box' restarts within 5 minutes. Log: $LogPath"
} else {
    Write-Log "login probe inconclusive: $($probe.why)"
}
exit 0
