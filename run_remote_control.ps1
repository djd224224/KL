# Launcher for "KL remote-control" -- the always-on Remote Control host that
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
# expired OAuth token -- it kept "running", it just never worked. A restart
# loop cannot fix a dead token; it only converts one clear failure into an
# endless quiet one. So: check first, alert, exit.
#
# NEVER UNREACHABLE (Jack 2026-10-12: "i do want to use remote control when i
# dont have access to PC. ensure that im never in a position where i cant
# access"). That day the host had been down since 9/26 ~noon: the launcher ran
# in a VISIBLE console, it died with 0xC000013A (the window closed -- the same
# way the IMM bot died on 9/28), and the task's only trigger was logon, so
# nothing brought it back for 16 days and nothing said so. Now:
#   - the task runs this through run_remote_control_hidden.vbs: no window to
#     close, and wscript waits on it, so the task is "Running" exactly while
#     this launcher lives;
#   - the task also fires every 5 minutes; MultipleInstances IgnoreNew makes
#     that a no-op while this runs and a restart within 5 min when it doesn't;
#   - the preflight is a REAL request (Test-ClaudeLogin, remote_control_common
#     .ps1): `claude auth status` said loggedIn=true all through the 10/12
#     login expiry. A dead login emails at once (then every 6h) and exits; the
#     5-minute trigger retries, so the host comes back by itself after a
#     sign-in, with an all-clear email;
#   - a login error the host prints while running (the expired-token 401
#     arrives as plain text) emails too and restarts the host into the
#     preflight -- a host that outlives its login stalls without exiting;
#   - remote_control_check.ps1 (task "KL remote-control check", hourly) probes
#     the login and starts this task if it is down, as a second line.
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
# Start by hand:  Start-ScheduledTask -TaskName "KL remote-control"
#                 (or: powershell -NoProfile -ExecutionPolicy Bypass -File run_remote_control.ps1)
# Stop:           Stop-ScheduledTask -TaskName "KL remote-control" -- but the
#                 5-minute trigger starts it again; disable the task to keep it down.

$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$SessionName = "trading-box"

Set-Location $Repo
$LogDir = Join-Path $Repo "run-logs\remote-control"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("{0:yyyy-MM-dd}.log" -f (Get-Date))

. (Join-Path $Repo "remote_control_common.ps1")

Write-Log "=== remote-control launcher start (session '$SessionName') ==="

# The host must run on the full /login: a setup-token can't establish Remote
# Control, and an API key outranks the login. Keep all three out of its env.
foreach ($v in 'CLAUDE_CODE_OAUTH_TOKEN','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN') {
    Remove-Item "Env:$v" -ErrorAction SilentlyContinue
}

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
        foreach ($h in (Get-HostProcesses $SessionName)) {
            $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($h.ParentProcessId)" -ErrorAction SilentlyContinue
            if ($parent) { continue }
            Stop-Process -Id $h.ProcessId -Force -ErrorAction Stop
            Write-Log "reaped orphaned host pid=$($h.ProcessId) (parent gone)"
        }
    } catch {
        Write-Log "orphan sweep failed (non-fatal): $_"
    }
}

# This launcher's own host (its child), stopped to end a run early.
function Stop-OwnHost {
    foreach ($h in (Get-HostProcesses $SessionName)) {
        if ($h.ParentProcessId -eq $PID) {
            Stop-Process -Id $h.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Log "stopped own host pid=$($h.ProcessId)"
        }
    }
}

Remove-OrphanedHosts


# --- Supervised run ----------------------------------------------------------
# The bridge self-heals across the laptop's Modern Standby naps, so a process
# exit means something real went wrong. Restart with a widening backoff, and
# give up loudly rather than spinning forever: a run that dies immediately,
# five times running, is a broken install or a revoked token, not a blip.
# Giving up exits; the task's 5-minute trigger starts a fresh launcher, and the
# give-up email is throttled to every 6h.
$backoff = 15
$rapidFailures = 0

while ($true) {
    # --- Preflight: does the login actually work? ----------------------------
    $probe = Test-ClaudeLogin
    if ($probe.auth) {
        Write-Log "NOT STARTED: the claude login is dead ($($probe.why))"
        Send-AlertThrottled 'login' 6 "remote-control DOWN: claude login expired" `
            ($LoginDeadBody + "`n`nLog: $LogPath")
        exit 1
    }
    if ($probe.ok) {
        Write-Log "login ok; starting host"
        Clear-Alert 'login' "remote-control: claude login OK again" `
            "The claude login on the trading box works again; the Remote Control host 'trading-box' is starting. Log: $LogPath"
    } else {
        Write-Log "login probe inconclusive ($($probe.why)); starting the host anyway"
    }

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
                        Clear-Alert 'giveup' "remote-control back up" `
                            "The Remote Control host 'trading-box' is connected and registered with claude.ai again. Log: $LogPath"
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

            # A login error while running: the host stalls on a dead login
            # without exiting, so end this run; the next preflight alerts (or
            # finds the login fine and restarts the host).
            if ($line -match $LoginErrorRe) {
                Write-Log "host printed a login error; restarting it into the preflight"
                Stop-OwnHost
            }
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
        Write-Log "giving up after 5 rapid failures; the task's 5-minute trigger retries"
        Send-AlertThrottled 'giveup' 6 "remote-control host giving up (5 rapid failures)" `
            ("The Remote Control host on the trading box exited 5 times in a row " +
             "without staying up 10 minutes. The box is NOT reachable from mobile " +
             "through 'trading-box' right now; the task retries every 5 minutes and " +
             "you get a 'back up' email when it connects.`n`n" +
             "Last exit code: $code`n" +
             "Log: $LogPath`n`n" +
             ((Get-Content $LogPath -Tail 15) -join "`n"))
        exit $code
    }

    Write-Log "restarting in $backoff s"
    Start-Sleep -Seconds $backoff
    $backoff = [Math]::Min($backoff * 2, 300)
}
