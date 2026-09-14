# Launcher for "KL remote-control" — the always-on Remote Control host that
# makes this box reachable from the Claude mobile app / claude.ai/code.
#
# WHAT IT IS: `claude remote-control` is a persistent server, not a one-shot.
# It registers this machine with claude.ai and then accepts sessions spawned
# from the phone, all rooted in this repo. It is the trading box's front door,
# so it is deliberately boring: default permission mode, supervised restarts,
# and it refuses to run blind.
#
# PERMISSION MODE is `default` on purpose. A phone session gets the same
# prompts an interactive session would, and the prompt pushes to the phone
# (inputNeededNotifEnabled). On a box holding live Kalshi positions, nothing
# restarts a fleet or cancels an order without a tap. Do NOT "simplify" this
# to bypassPermissions.
#
# THE AUTH PREFLIGHT is the whole point of this script. From 2026-08-10 to
# 2026-09-13 the kalshi-daily-recap task failed 35 mornings straight on one
# expired OAuth token — it kept "running", it just never worked. A restart
# loop cannot fix a dead token; it only converts one clear failure into an
# endless quiet one. So: check first, alert, exit. Note that `claude auth
# status` exits 0 even when signed out, so the check reads the JSON body.
#
# Logs to run-logs\remote-control\YYYY-MM-DD.log.
# --spawn IS NOT OPTIONAL HERE even though same-dir is the documented default.
# On its first run in a project the host asks "Spawn mode [1/2]" on stdin. Under
# Task Scheduler there is no stdin, so it parks on that prompt forever: the task
# reports Running, the log stops mid-banner, and the box never registers. Passing
# the flag skips the question. (The consent prompt has the same shape but is
# remembered in ~/.claude.json as remoteDialogSeen; this one is per project.)
#
# same-dir, not worktree: the point of reaching this box from a phone is
# operational -- read run-logs, check a fleet, restart one -- and that work wants
# the live tree the scheduled tasks actually run from. The cost is that a phone
# session edits the same tree the 30-min sync ff-merges into, so treat phone
# edits as disposable and commit promptly. Switch to --spawn worktree if you
# ever want isolated checkouts per session instead.
#
#
# Start by hand:  powershell -NoProfile -ExecutionPolicy Bypass -File run_remote_control.ps1
# Stop:           stop the "KL remote-control" scheduled task (or Ctrl+C).

$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$SessionName = "trading-box"

Set-Location $Repo
$LogDir = Join-Path $Repo "run-logs\remote-control"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("{0:yyyy-MM-dd}.log" -f (Get-Date))

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
    } catch {
        Write-Log "alert email itself failed: $_"
    }
}

Write-Log "=== remote-control launcher start (session '$SessionName') ==="

# --- Preflight: is the CLI actually signed in? -------------------------------
$loggedIn = $false
try {
    $status = & $Claude auth status 2>&1 | Out-String
    $loggedIn = ([bool](($status | ConvertFrom-Json).loggedIn))
} catch {
    Write-Log "auth status unreadable: $_"
}

if (-not $loggedIn) {
    Write-Log "NOT STARTED: claude CLI is not signed in."
    Send-Alert "remote-control NOT STARTED: claude CLI signed out" `
        ("The Remote Control host on the trading box did not start because the " +
         "claude CLI is signed out, so the box is NOT reachable from mobile.`n`n" +
         "Fix, in a terminal on the box:`n" +
         "    claude auth login`n`n" +
         "The same token gates the 5:45 AM kalshi-daily-recap task.`n" +
         "Log: $LogPath")
    exit 1
}
Write-Log "auth ok; starting host"

# --- Reap orphaned hosts ------------------------------------------------------
# Stop-ScheduledTask kills the launcher but NOT the claude.exe it spawned, so
# every stop/restart leaves a live host behind holding its own registration.
# This box has been here before: the old desktop sync task leaked a claude.exe
# every 30 minutes until ~66 zombies were eating ~5 GB.
#
# Deliberately narrow, in the spirit of enable_updown_daily.ps1: a process is
# only touched if its command line proves it is one of OUR hosts (remote-control
# AND this session name) AND its parent is gone. A host with a live parent is
# supervised by somebody -- leave it alone.
function Remove-OrphanedHosts {
    try {
        $hosts = Get-CimInstance Win32_Process -Filter "Name='claude.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -and
                           $_.CommandLine -like '*remote-control*' -and
                           $_.CommandLine -like "*$SessionName*" }
        foreach ($h in $hosts) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($h.ParentProcessId)" -ErrorAction SilentlyContinue
            if ($parent) { continue }
            Stop-Process -Id $h.ProcessId -Force -ErrorAction Stop
            Write-Log "reaped orphaned host pid=$($h.ProcessId) (parent gone)"
        }
    } catch {
        Write-Log "orphan sweep failed (non-fatal): $_"
    }
}

Remove-OrphanedHosts


# --- Supervised run ----------------------------------------------------------
# The bridge self-heals across the laptop's Modern Standby naps, so a process
# exit means something real went wrong. Restart with a widening backoff, and
# give up loudly rather than spinning forever: a run that dies immediately,
# five times running, is a broken install or a revoked token, not a blip.
$backoff = 15
$rapidFailures = 0

while ($true) {
    $startedAt = Get-Date
    Write-Log "launching: claude remote-control --name $SessionName --permission-mode default --spawn same-dir"

    # Do NOT tee the host's stdout straight into the log. It paints a live TUI
    # status banner, and against a file every repaint appends a fresh frame:
    # measured at 18 KB per 30 s -- ~49 MB/day of escape codes, with the lines
    # that actually matter buried in them. Keep the signal, drop the frames:
    # anything carrying an ESC is a repaint (logged once, the first time it
    # says Connected, so the log still proves registration); plain text is a
    # real message (the expired-token 401 arrived as plain text) and is kept,
    # deduped.
    $connectedLogged = $false
    $seenHostLines = [System.Collections.Generic.HashSet[string]]::new()

    & $Claude remote-control --name $SessionName --permission-mode default --spawn same-dir 2>&1 |
        ForEach-Object {
            $line = [string]$_
            if ([string]::IsNullOrWhiteSpace($line)) { return }

            if ($line.Contains([char]27)) {
                if (-not $connectedLogged) {
                    $clean = [regex]::Replace($line, "$([char]27)\[[0-9;?]*[A-Za-z]", '')
                    if ($clean -match 'Connected') {
                        $connectedLogged = $true
                        Write-Log "host: connected and registered with claude.ai"
                    }
                }
                return
            }

            # Log each DISTINCT line once per run, not just consecutive dupes.
            # The banner's text rows ("Capacity: 1/32", "Continue coding in the
            # Claude mobile app...") carry no ESC and alternate with each other,
            # so a consecutive-only check lets the whole frame through on every
            # repaint -- which is most of the 49 MB. A status line repeated
            # verbatim is never new information; a genuinely new message is.
            if (-not $seenHostLines.Add($line)) { return }
            if ($seenHostLines.Count -gt 500) { $seenHostLines.Clear() }
            Write-Log "host: $line"
        }

    $code = $LASTEXITCODE
    $ranFor = (Get-Date) - $startedAt
    Write-Log ("host exited code={0} after {1:n1} min" -f $code, $ranFor.TotalMinutes)

    # A run that held for 10+ minutes was a real session, not a boot failure.
    if ($ranFor.TotalMinutes -ge 10) {
        $backoff = 15
        $rapidFailures = 0
    } else {
        $rapidFailures++
    }

    if ($rapidFailures -ge 5) {
        Write-Log "giving up after 5 rapid failures"
        Send-Alert "remote-control host giving up (5 rapid failures)" `
            ("The Remote Control host on the trading box exited 5 times in a row " +
             "without staying up 10 minutes. The box is NOT reachable from mobile.`n`n" +
             "Last exit code: $code`n" +
             "Log: $LogPath`n`n" +
             ((Get-Content $LogPath -Tail 15) -join "`n"))
        exit $code
    }

    Write-Log "restarting in $backoff s"
    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min($backoff * 2, 300)
}
