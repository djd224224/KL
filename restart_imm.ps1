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
# scheduled-task bounce (stop task, sweep the surviving chain, start task) —
# REQUIRED when the launcher's $ProbeEnv changed: a python kill keeps the
# launcher's stale env (the 2026-08-01 gotcha), and Stop-ScheduledTask can
# orphan the python child (observed same day), hence the sweep. Since
# 2026-09-28 the task runs the launcher under a hidden wscript wrapper
# (run_incentive_mm_hidden.vbs -- the visible console was closed by accident
# and took the bot with it), so ending the task can orphan the LAUNCHER as
# well, and an orphaned launcher relaunches python 30s after the sweep, next
# to the fresh task's bot: two bots on one account. The sweep therefore
# takes the whole chain -- launcher, cmd shim, wrapper, python, in that
# order so nothing respawns mid-pass -- and refuses to start the task while
# any of it survives.
# -Now skips the window wait (emergencies).
#
# THE BOOK IS HANDED OVER, NOT CANCELLED (Jack 2026-10-01, "both"). Either
# mode used to kill python outright, so the next process cancelled every
# leftover order and rebuilt the book at the per-cycle placement cap: the
# 10/1 14:59Z -Task bounce cancelled 1,391 orders and the book was back only
# ~15:19Z. Now, with a bot running, the script drops
# run-logs\incentive-mm\restart_handoff_request.json and waits (up to
# -HandoffWaitSecs) for the bot to finish its cycle, hand its resting book
# over (restart_handoff.json, the same handoff a code-change exit makes) and
# exit; the relaunch adopts the book. -Task first kills the launcher chain
# (launcher, cmd shim, wrapper -- python keeps trading meanwhile) so nothing
# can relaunch python on the OLD env, then starts the task once python is
# gone. Default mode leaves the launcher alone, which relaunches python ~30s
# after the handoff exit. A bot that has not exited by the deadline is
# killed exactly as before (its relaunch cancels the leftovers). -NoHandoff
# restores the old hard kill.

param(
    [switch]$Task,
    [switch]$Now,
    # the old hard kill: no handoff request, the relaunch cancels the book
    [switch]$NoHandoff,
    # how long to wait for the bot to finish its cycle, hand over and exit
    [int]$HandoffWaitSecs = 600,
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

function Get-ImmChain {
    # Every live process of the bot chain, in kill order: the launcher
    # powershell (rank 0: it relaunches python, so it dies first), the cmd
    # shim it runs python through (1), the hidden wscript wrapper (2), the
    # bot python (3). Matched on command lines -- a launcher inside its 30s
    # relaunch sleep has no python child to walk up from -- plus each bot
    # python's parent cmd. Never a process in this script's own ancestry.
    $procs = @(Get-CimInstance Win32_Process -Filter ("Name like '%python%' or " +
        "Name = 'cmd.exe' or Name = 'powershell.exe' or Name = 'pwsh.exe' or " +
        "Name = 'wscript.exe'"))
    $byId = @{}
    foreach ($p in $procs) { $byId[[int]$p.ProcessId] = $p }
    $mine = @()
    $cur = $PID
    for ($i = 0; $i -lt 8 -and $cur; $i++) {
        $mine += $cur
        $me = Get-CimInstance Win32_Process -Filter "ProcessId=$cur"
        if (-not $me) { break }
        $cur = [int]$me.ParentProcessId
    }
    $rank = @{}
    foreach ($p in $procs) {
        $cl = [string]$p.CommandLine
        if ($p.Name -like 'python*' -and $cl -like '*incentive_mm.py*') {
            $rank[[int]$p.ProcessId] = 3
            $parent = $byId[[int]$p.ParentProcessId]
            if ($parent -and $parent.Name -eq 'cmd.exe') { $rank[[int]$parent.ProcessId] = 1 }
        } elseif ($p.Name -in @('powershell.exe', 'pwsh.exe') -and $cl -like '*run_incentive_mm.ps1*') {
            $rank[[int]$p.ProcessId] = 0
        } elseif ($p.Name -eq 'wscript.exe' -and $cl -like '*run_incentive_mm_hidden.vbs*') {
            $rank[[int]$p.ProcessId] = 2
        }
    }
    foreach ($m in $mine) { $rank.Remove([int]$m) }
    $rank.GetEnumerator() | Sort-Object Value | ForEach-Object {
        [pscustomobject]@{ ProcessId = $_.Key; Name = $byId[$_.Key].Name; Rank = $_.Value }
    }
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

function Invoke-BotHandoff {
    # Ask the running bot to hand its book over and exit (THE BOOK IS HANDED
    # OVER, above). $true once no bot python is left; $false when the request
    # could not be written or the bot is still running at the deadline -- the
    # caller then kills it as before. The request is written atomically (temp
    # file + rename) and removed again either way.
    $req = Join-Path $StatusDir 'restart_handoff_request.json'
    $tmp = "$req.tmp"
    $ts = ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() / 1000.0).ToString(
        'F3', [Globalization.CultureInfo]::InvariantCulture)
    Set-Content -Path $tmp -Value ('{"ts": ' + $ts + ', "by": "restart_imm.ps1"}') -Encoding Ascii
    Move-Item -Path $tmp -Destination $req -Force
    if (-not (Test-Path $req)) {
        Write-RLog "! handoff: could not write $req; falling back to the hard kill"
        return $false
    }
    Write-RLog "handoff: asked the bot to hand its book over and exit; waiting up to $HandoffWaitSecs s"
    $deadline = (Get-Date).AddSeconds($HandoffWaitSecs)
    while ((Get-Date) -lt $deadline -and @(Get-BotProcs).Count -gt 0) {
        Start-Sleep -Seconds 2
    }
    Remove-Item -Path $req -ErrorAction SilentlyContinue
    if (@(Get-BotProcs).Count -gt 0) {
        Write-RLog ("! handoff: bot still running after $HandoffWaitSecs s; killing " +
                    "it (its relaunch cancels the leftover orders)")
        return $false
    }
    if (Test-Path (Join-Path $StatusDir 'restart_handoff.json')) {
        Write-RLog "handoff: bot exited and handed its book over (restart_handoff.json)"
    } else {
        Write-RLog "handoff: bot exited WITHOUT a handoff (its exit cancelled the book)"
    }
    return $true
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
    if (-not $NoHandoff -and @(Get-BotProcs).Count -gt 0) {
        # Launcher chain first and python left alone: the launcher would
        # relaunch python on the OLD env the moment it exits. Killing the
        # wrapper ends the task's own process, so the task falls back to Ready
        # -- and the watchdog still refuses to start it while python lives.
        Write-RLog ("task restart: handing over -- stopping the launcher chain " +
                    "of '$TaskName'; the bot trades on until it hands over")
        for ($pass = 1; $pass -le 4; $pass++) {
            $sup = @(Get-ImmChain | Where-Object { $_.Rank -lt 3 })
            if ($sup.Count -eq 0) { break }
            foreach ($p in $sup) {
                Write-RLog "  killing $($p.Name) (pid $($p.ProcessId))"
                Stop-Process -Id $p.ProcessId -Force
            }
            Start-Sleep -Seconds 2
        }
        if (@(Get-ImmChain | Where-Object { $_.Rank -lt 3 }).Count -gt 0) {
            Write-RLog "! launcher chain survived its sweep; no handoff -- hard bounce"
        } else {
            [void](Invoke-BotHandoff)
        }
    }
    Write-RLog "task restart: stopping '$TaskName'"
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    # Stop-ScheduledTask can leave the chain under the task's own process
    # alive -- the python child, still trading (2026-08-01), and under the
    # hidden wrapper the launcher itself, which would relaunch python next
    # to the fresh task's bot. Sweep it all before starting or two bots
    # collide on one account.
    for ($pass = 1; $pass -le 4; $pass++) {
        $chain = @(Get-ImmChain)
        if ($chain.Count -eq 0) { break }
        foreach ($p in $chain) {
            Write-RLog "  killing surviving $($p.Name) (pid $($p.ProcessId))"
            Stop-Process -Id $p.ProcessId -Force
        }
        Start-Sleep -Seconds 2
    }
    $left = @(Get-ImmChain)
    if ($left.Count -gt 0) {
        Write-RLog ("! bot chain still alive after the sweep (pid " +
                    (($left | ForEach-Object { $_.ProcessId }) -join ',') +
                    "); NOT starting '$TaskName' -- it would duplicate the bot")
        exit 1
    }
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
    if (-not $NoHandoff) {
        # the launcher stays up and relaunches python ~30s after the handoff
        # exit; the relaunch adopts the book
        if (Invoke-BotHandoff) { exit 0 }
        $procs = Get-BotProcs      # deadline passed: kill it as before
    }
    foreach ($p in $procs) {
        Write-RLog "killing incentive_mm python (pid $($p.ProcessId)); launcher relaunches in ~30s with fresh code"
        Stop-Process -Id $p.ProcessId -Force
    }
}
