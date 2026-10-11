# One-supervisor-per-market rules for the crypto bot launchers, dot-sourced by
# run_crypto_touch_mm.ps1, run_crypto_touch_mm_weekly.ps1,
# run_crypto_updown_mm.ps1 and run_crypto_annual_mm.ps1.
# Runs under Windows PowerShell 5.1: no ?? / ternary here.
#
# WHY (2026-10-11). Ending a scheduled task kills only the process Task
# Scheduler started: the hidden wscript wrapper. The launcher under it and
# its python bot ran on, orphaned, and the launcher kept relaunching the bot.
# The 15-min watchdog then started the Ready task again, and the new
# launcher's python exited on the bot's singleton lock ("another instance is
# live") every ~30s, forever. The annual/updown tasks were registered with
# Task Scheduler's default 72h time limit, so this happened every 3 days: by
# 10/11 each of their 15 assets had 3 idle launchers (45 x ~60MB), together
# starting ~85k pythons a day on a laptop that was out of RAM.
#
# Before every bot start the launcher now checks, in this order:
#  1. Task ended. A launcher started by its task's wrapper lives only as long
#     as that wrapper. Once the wrapper is gone the launcher exits, also while
#     its bot is running: the bot is left alone and the task's next instance
#     (the watchdog starts it within 15 min) adopts it under rule 3.
#  2. An OLDER launcher for the same market is alive (one from before this
#     fix, or a hand-started one): stand by until it exits. It relaunches its
#     own bot, and racing it for the lock would turn it into a respawner.
#  3. A bot for the same market is already running without a launcher: wait
#     for it to exit, rather than start a python that the lock will refuse.
# Launchers never stop another process.
#
# Hand-run launchers (no wrapper parent) skip rule 1 and keep rules 2-3.
#
# These lines go to the fleet's launcher-guard.log, not the day log: while a
# bot runs, cmd's ">>" redirect holds its day log and refuses other writers.

function Initialize-LauncherGuard {
    param(
        [string]$Launcher,   # this launcher's file name, e.g. run_crypto_annual_mm.ps1
        [string]$Wrapper,    # its hidden wscript wrapper, e.g. run_crypto_annual_mm_hidden.vbs
        [string]$Bot,        # the bot module, e.g. crypto_annual_mm.py
        [string]$ArgName,    # asset | market: the launcher's -Asset is the bot's --asset
        [string]$ArgValue,
        [string]$GuardLog    # e.g. run-logs\crypto-annual\launcher-guard.log
    )
    $v = [regex]::Escape($ArgValue)
    # A path separator, quote or space before the file name keeps
    # crypto_touch_mm.py from matching crypto_touch_mm_weekly.py / test_*.
    $script:GuardLauncherRx = '(?i)(^|[\\/"\s])' + [regex]::Escape($Launcher) +
                              '"?\s.*-' + $ArgName + '\s+"?' + $v + '"?(\s|$)'
    $script:GuardBotRx = '(?i)(^|[\\/"\s])' + [regex]::Escape($Bot) +
                         '"?\s.*--' + $ArgName + '\s+"?' + $v + '"?(\s|$)'
    $script:GuardLog = $GuardLog
    $script:GuardKey = $ArgValue
    $script:GuardStart = $null
    $script:GuardParent = $null
    $script:GuardParentDesc = ""
    $script:GuardWaitingOn = 0

    $me = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction SilentlyContinue
    if (-not $me) { return }
    $script:GuardStart = $me.CreationDate
    $par = Get-CimInstance Win32_Process -Filter "ProcessId=$($me.ParentProcessId)" -ErrorAction SilentlyContinue
    # Task-run = our own wrapper, and older than us (a younger "parent" is a
    # recycled PID). Hold a handle so the PID can't be recycled under us.
    if ($par -and $par.Name -eq "wscript.exe" -and $par.CommandLine -like "*$Wrapper*" -and
            $par.CreationDate -le $me.CreationDate) {
        try {
            $p = [System.Diagnostics.Process]::GetProcessById([int]$par.ProcessId)
            $null = $p.Handle
            $script:GuardParent = $p
            $script:GuardParentDesc = "wrapper wscript pid $($par.ProcessId)"
        } catch {}
    }
}

function Write-GuardLog([string]$Message) {
    # Real UTC, unlike the day logs' launcher lines. A few tries: the fleet's
    # launchers share this file.
    $line = "{0} {1} launcher pid {2}: {3}" -f (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ssZ"),
            $script:GuardKey, $PID, $Message
    for ($i = 0; $i -lt 3; $i++) {
        try { Add-Content -Path $script:GuardLog -Value $line -ErrorAction Stop; return } catch { Start-Sleep -Milliseconds 200 }
    }
}

function Test-TaskEnded {
    # Rule 1: true once this launcher's wrapper is gone. Hand-run: never.
    if (-not $script:GuardParent) { return $false }
    try { return $script:GuardParent.HasExited } catch { return $false }
}

function Get-OlderLauncher {
    # Rule 2: the oldest other launcher for this market that started before us.
    if (-not $script:GuardStart) { return $null }
    $older = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction SilentlyContinue |
               Where-Object { $_.ProcessId -ne $PID -and $_.CreationDate -lt $script:GuardStart -and
                              $_.CommandLine -match $script:GuardLauncherRx } |
               Sort-Object CreationDate)
    if ($older.Count -gt 0) { return $older[0] }
    return $null
}

function Get-RunningBot {
    # Rule 3: a python already running this market's bot (live or not).
    $bots = @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -match $script:GuardBotRx } |
              Sort-Object CreationDate)
    if ($bots.Count -gt 0) { return $bots[0] }
    return $null
}

function Wait-OtherProcess($Proc, [string]$Rx) {
    # Block until $Proc (a Win32_Process record) exits. $false = this
    # launcher's task ended first (the caller exits); $true otherwise.
    try { $p = [System.Diagnostics.Process]::GetProcessById([int]$Proc.ProcessId) } catch { return $true }
    try {
        # A held handle pins the PID (not possible on an elevated process);
        # the re-read makes sure a PID recycled since the CIM read is never
        # mistaken for the process we meant.
        try { $null = $p.Handle } catch {}
        $now = Get-CimInstance Win32_Process -Filter "ProcessId=$($Proc.ProcessId)" -ErrorAction SilentlyContinue
        if (-not $now -or $now.CommandLine -notmatch $Rx) { return $true }
        while (-not $p.WaitForExit(15000)) {
            if (Test-TaskEnded) { return $false }
        }
        return $true
    } catch {
        Start-Sleep -Seconds 15    # an unexpected error must never turn into a spin
        return $true
    } finally {
        $p.Dispose()
    }
}

function Wait-LauncherTurn {
    # Rules 1-3. $true = start the bot now; $false = the task ended (exit).
    while ($true) {
        if (Test-TaskEnded) {
            Write-GuardLog "task ended ($script:GuardParentDesc gone); exiting"
            return $false
        }
        $older = Get-OlderLauncher
        if ($older) {
            # Log each new wait target once, never once per re-check.
            if ($script:GuardWaitingOn -ne $older.ProcessId) {
                $since = $older.CreationDate.ToString("yyyy-MM-dd HH:mm")
                Write-GuardLog ("standing by: launcher pid $($older.ProcessId) (started $since) " +
                                "runs this bot; taking over when it exits")
                $script:GuardWaitingOn = $older.ProcessId
            }
            [void](Wait-OtherProcess $older $script:GuardLauncherRx)
            continue
        }
        $bot = Get-RunningBot
        if ($bot) {
            if ($script:GuardWaitingOn -ne $bot.ProcessId) {
                Write-GuardLog "bot pid $($bot.ProcessId) is already running; adopting it (relaunch when it exits)"
                $script:GuardWaitingOn = $bot.ProcessId
            }
            if (Wait-OtherProcess $bot $script:GuardBotRx) {
                if (-not (Get-RunningBot)) {
                    Write-GuardLog "adopted bot pid $($bot.ProcessId) exited; starting a fresh one in 30s"
                    Start-Sleep -Seconds 30
                }
            }
            continue
        }
        $script:GuardWaitingOn = 0
        return $true
    }
}

function Invoke-GuardedBot([string]$Command, [string]$WorkDir) {
    # Run the bot's cmd.exe line, the same line the launchers ran with
    # "& cmd.exe /c", and wait for it. Returns its exit code, or $null when
    # this launcher's task ended first: the launcher then exits and the bot
    # runs on for the task's next instance to adopt.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $env:ComSpec
    if (-not $psi.FileName) { $psi.FileName = "cmd.exe" }
    $psi.Arguments = "/c `"$Command`""
    $psi.UseShellExecute = $false      # inherit this hidden console, like "&" did
    $psi.WorkingDirectory = $WorkDir   # Set-Location doesn't move the process cwd
    try { $p = [System.Diagnostics.Process]::Start($psi) }
    catch { Write-GuardLog "! could not start the bot: $($_.Exception.Message)"; return -1 }
    try {
        while (-not $p.WaitForExit(15000)) {
            if (Test-TaskEnded) { return $null }
        }
        return $p.ExitCode
    } finally {
        $p.Dispose()
    }
}
