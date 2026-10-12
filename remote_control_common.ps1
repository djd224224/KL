# Shared by run_remote_control.ps1 (the Remote Control host launcher) and
# remote_control_check.ps1 (its hourly check). Dot-source it AFTER setting
# $Repo, $Claude, $LogDir and $LogPath.
#
# Jack 2026-10-12: "i do want to use remote control when i dont have access to
# PC. ensure that im never in a position where i cant access". That day the
# host had been down since 9/26 and nothing said so, and the /login it runs on
# had expired ("OAuth session expired and could not be refreshed") while
# `claude auth status` still reported loggedIn=true.

# ASCII only: Windows PowerShell 5.1 reads a BOM-less script as ANSI.

function Write-Log($msg) {
    "{0:u} {1}" -f (Get-Date), $msg | Add-Content -Path $LogPath -Encoding utf8
}

# Task Scheduler sessions do not reliably inherit user env vars (same reason
# the recap launcher pulls its Kalshi key from HKCU).
function Import-AlertEnv {
    foreach ($n in 'ALERT_EMAIL_FROM','ALERT_EMAIL_PASSWORD','ALERT_EMAIL_TO') {
        if (-not (Get-Item "env:$n" -ErrorAction SilentlyContinue)) {
            try {
                Set-Item "env:$n" (Get-ItemProperty -Path "HKCU:\Environment" -Name $n -ErrorAction Stop).$n
            } catch {}
        }
    }
}

function Send-Alert($subject, $body) {
    Import-AlertEnv
    try {
        python (Join-Path $Repo "send_alert_email.py") $subject $body 2>&1 |
            Add-Content -Path $LogPath -Encoding utf8
        return ($LASTEXITCODE -eq 0)
    } catch {
        Write-Log "alert email itself failed: $_"
        return $false
    }
}

# ALERT THROTTLE: one file per trouble, run-logs\remote-control\alert_<key>.txt,
# holding when its email last went out. The 5-minute retry trigger relaunches
# a host that keeps failing, so without this a dead login would email every 5
# minutes. Re-sent every $hours while the trouble lasts; Clear-Alert re-arms it
# and sends one all-clear.
function Send-AlertThrottled($key, $hours, $subject, $body) {
    $f = Join-Path $LogDir "alert_$key.txt"
    if (Test-Path $f) {
        try {
            $last = [datetime]::Parse((Get-Content -Raw $f).Trim(), $null,
                [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime()
            if (((Get-Date).ToUniversalTime() - $last).TotalHours -lt $hours) {
                Write-Log "alert '$key' held (last sent $($last.ToString('u')))"
                return
            }
        } catch {}
    }
    if (Send-Alert $subject $body) {
        (Get-Date).ToUniversalTime().ToString('o') | Set-Content -Path $f -Encoding ascii
    }
}

function Clear-Alert($key, $subject, $body) {
    $f = Join-Path $LogDir "alert_$key.txt"
    if (-not (Test-Path $f)) { return }
    Remove-Item $f -ErrorAction SilentlyContinue
    Write-Log "alert '$key' cleared"
    if ($subject) { [void](Send-Alert $subject $body) }
}

# What a dead login looks like in the CLI's output (10/12: "Failed to
# authenticate: OAuth session expired and could not be refreshed"; the host
# printed an expired-token 401 as plain text before).
$LoginErrorRe = 'failed to authenticate|oauth (session|token)|could not be refreshed|' +
                'not logged in|run /login|log ?in again|sign ?in again|' +
                'authentication_error|invalid api key|\b401\b'

# LOGIN PROBE: one REAL request on the stored /login credentials. `claude auth
# status` cannot tell a dead login from a live one (it said loggedIn=true all
# through the 10/12 outage). Remote Control needs the full /login -- a `claude
# setup-token` token can't establish it -- so the probe drops the token and
# API-key variables to test exactly the login the host runs on. Haiku, no MCP,
# no saved session: a few cents of plan usage.
# Returns @{ ok; auth; why }. auth = the LOGIN is dead (alert). ok and auth
# both false = timeout / network: the host reconnects by itself when the
# network returns, so that is no reason to keep it down.
function Test-ClaudeLogin {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Claude
    $psi.Arguments = '-p "Reply with the single word OK." --output-format text ' +
                     '--model haiku --no-session-persistence --strict-mcp-config'
    $psi.WorkingDirectory = $Repo
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    foreach ($v in 'CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN') {
        if ($psi.EnvironmentVariables.ContainsKey($v)) { $psi.EnvironmentVariables.Remove($v) }
    }
    try {
        $p = [System.Diagnostics.Process]::Start($psi)
    } catch {
        return @{ ok = $false; auth = $false; why = "probe could not start: $_" }
    }
    $p.StandardInput.Close()
    $o = $p.StandardOutput.ReadToEndAsync()
    $e = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit(180000)) {
        try { $p.Kill() } catch {}
        return @{ ok = $false; auth = $false; why = "probe timed out after 3 min" }
    }
    $out = [regex]::Replace(($o.Result + "`n" + $e.Result), "$([char]27)\[[0-9;?]*[A-Za-z]", '').Trim()
    if ($p.ExitCode -eq 0 -and $out -match '\bOK\b') {
        return @{ ok = $true; auth = $false; why = "" }
    }
    $tail = (($out -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 2) -join ' | '
    return @{ ok = $false; auth = [bool]($out -match $LoginErrorRe); why = "exit $($p.ExitCode): $tail" }
}

# The host process(es) for this session name, as [CimInstance]s.
function Get-HostProcesses($sessionName) {
    # PowerShell unrolls a returned array, and a lone CimInstance has no
    # usable .Count: callers wrap the call in @() before counting.
    try {
        return @(Get-CimInstance Win32_Process -Filter "Name='claude.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -and $_.CommandLine -like '*remote-control*' -and
                           $_.CommandLine -like "*$sessionName*" })
    } catch {
        return @()
    }
}

# A forced restart stops the WHOLE chain first. Ending the task kills the
# wscript wrapper but is not guaranteed to take the powershell launcher under
# it (the IMM bot's wrapper notes the same, 2026-08-01), and a surviving
# launcher keeps relaunching its host: a second host would register
# 'trading-box' twice. So: every run_remote_control.ps1 powershell, then its
# hosts, then the task.
function Stop-LauncherChain($taskName, $sessionName) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    try {
        Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -and $_.CommandLine -like '*run_remote_control.ps1*' -and
                           $_.ProcessId -ne $PID } |
            ForEach-Object {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                Write-Log "stopped launcher pid=$($_.ProcessId)"
            }
    } catch {
        Write-Log "launcher sweep failed (non-fatal): $_"
    }
    foreach ($h in (Get-HostProcesses $sessionName)) {
        Stop-Process -Id $h.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Log "stopped host pid=$($h.ProcessId)"
    }
}

$LoginDeadBody = ("The claude CLI login on the trading box has expired, so the Remote " +
    "Control host 'trading-box' can NOT be reached from your phone until you " +
    "sign in again on the PC:`n`n" +
    "    claude auth login`n`n" +
    "The host retries every 5 minutes by itself and comes back on its own " +
    "once the login works; you get an all-clear email then.`n`n" +
    "Still reachable meanwhile, probably: sessions open in the Claude desktop " +
    "app (it signs in on its own) and any remote-desktop tool you set up.`n" +
    "Not affected: the IMM research and the daily recap, which run on the " +
    "one-year token in ~/.claude/imm-research-token.txt.")
