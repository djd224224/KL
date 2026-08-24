# Windowed restart for the live IMM bot (Jack 2026-08-24: "always restart
# bot in the :45 - 1:05 timeframe, if there is an hourly temp market. since
# hourly temp isnt quoted" — window shifted later the same day, Jack:
# "actually shift to :50 - :05").
#
# WHY THE WINDOW. Hourly temp (KXTEMP<CITY>H) is the richest family in the
# feed and its quoting lives mid-hour: the program activates ~hh:11 and the
# close-anchored cutoff ends quoting ~hh:50 (close-10). Between :50 and the
# next :11 the family has nothing at risk, so a restart there is free;
# a mid-hour restart forfeits quoting minutes on the top pools and re-clears
# floors from scratch. The :50-:05 window IS that dead zone: at :50 the
# hour's quotes are at their cutoff-capped expiry (killing python does NOT
# cancel resting orders — they die server-side at TTL/cutoff), and a kill
# at :05 has the bot back ~:06, ahead of the ~:11 activation.
# Minute-of-hour is timezone-agnostic (ET is a whole-hour offset), so local
# clock minutes are exactly ET minutes.
#
# WHEN THE WINDOW APPLIES. Only while hourly temp is actually in play,
# detected from the bot's own state: selected_tickers in imm_state.json
# (rewritten every cycle) matching ^KXTEMP[A-Z]+H-. Temp absent (program
# hours over, family dark) -> restart immediately, nothing to protect.
# Bot process not running at all -> also immediately: a down bot quotes
# nothing, and waiting 40 minutes to revive it would be the expensive
# direction. Unreadable state with a live process fails toward WAITING.
#
# WHAT IT RESTARTS. Default: kill the incentive_mm.py python; the launcher
# (run_incentive_mm.ps1) relaunches it ~30s later with freshly imported
# code — the right tool after a sync-kl-main code pull. -Task: full
# scheduled-task bounce (stop task, sweep surviving pythons, start task) —
# REQUIRED when the launcher's $ProbeEnv changed: a python kill keeps the
# launcher's stale env (the 2026-08-01 gotcha), and Stop-ScheduledTask can
# orphan the python child (observed same day), hence the sweep.
# -Now skips the window wait (emergencies).

param(
    [switch]$Task,
    [switch]$Now,
    # Parameterized ONLY so a throwaway task can exercise -Task safely;
    # production callers pass nothing (same convention as the watchdog).
    [string]$TaskName = 'KL incentive_mm'
)

$ErrorActionPreference = 'SilentlyContinue'
$Repo = 'C:\Users\jackd\Documents\KL'
$StatusDir = Join-Path $Repo 'run-logs\incentive-mm'
$LogPath = Join-Path $StatusDir 'restart-imm.log'
New-Item -ItemType Directory -Force $StatusDir | Out-Null

function Write-RLog([string]$Message) {
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ssZ')
    Add-Content -Path $LogPath -Value "$stamp $Message"
    Write-Host $Message
}

function Get-BotProcs {
    @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
      Where-Object { $_.CommandLine -like '*incentive_mm.py*' })
}

function Test-InWindow {
    $m = (Get-Date).Minute
    return ($m -ge 50) -or ($m -le 5)
}

function Test-HourlyTempLive {
    # Unreadable state while a bot is running -> $true (fail toward the
    # window: waiting costs at most ~40 min; a mid-hour restart on live
    # temp costs the top pools). Missing file -> $false (bot never ran
    # here; nothing to protect).
    $stateFile = Join-Path $StatusDir 'imm_state.json'
    if (-not (Test-Path $stateFile)) { return $false }
    try {
        $state = Get-Content $stateFile -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        return @($state.selected_tickers | Where-Object { $_ -match '^KXTEMP[A-Z]+H-' }).Count -gt 0
    } catch {
        return $true
    }
}

$procs = Get-BotProcs
if ($procs.Count -eq 0 -and -not $Task) {
    Write-RLog ("no incentive_mm process found - nothing to kill; the " +
                "launcher relaunches a crashed bot itself and the watchdog " +
                "revives a dead task (or re-run with -Task to bounce the task)")
    exit 0
}

if (-not $Now -and $procs.Count -gt 0 -and (Test-HourlyTempLive) -and -not (Test-InWindow)) {
    $now = Get-Date
    $target = $now.Date.AddHours($now.Hour).AddMinutes(50)
    $waitSecs = [int]([math]::Ceiling(($target - $now).TotalSeconds))
    Write-RLog ("hourly temp in play (imm_state.json) and outside the " +
                ":50-:05 window - waiting $waitSecs s until " +
                $target.ToString('HH:mm') + " (use -Now to skip)")
    Start-Sleep -Seconds $waitSecs
}

if ($Task) {
    $t = Get-ScheduledTask -TaskName $TaskName
    if (-not $t) { Write-RLog "! task '$TaskName' not found"; exit 1 }
    Write-RLog "task restart: stopping '$TaskName'"
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    # Stop-ScheduledTask can leave the python child orphaned and still
    # trading (2026-08-01); sweep before starting or two bots collide on
    # one account.
    foreach ($p in Get-BotProcs) {
        Write-RLog "  killing surviving incentive_mm python (pid $($p.ProcessId))"
        Stop-Process -Id $p.ProcessId -Force
    }
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $TaskName
    Write-RLog "task restart: '$TaskName' started (fresh launcher env + code)"
} else {
    # Re-fetch: the window wait can be ~40 min and the launcher may have
    # cycled the python (new pid) in the meantime.
    $procs = Get-BotProcs
    if ($procs.Count -eq 0) {
        Write-RLog "bot process gone after the window wait - launcher already cycling it; nothing to kill"
        exit 0
    }
    foreach ($p in $procs) {
        Write-RLog "killing incentive_mm python (pid $($p.ProcessId)); launcher relaunches in ~30s with fresh code"
        Stop-Process -Id $p.ProcessId -Force
    }
}
