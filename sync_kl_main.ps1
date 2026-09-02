# Plain git fast-forward sync of KL main. Replaces the Claude Desktop
# 'sync-kl-main' scheduled task (disabled 2026-07-08 after its agent runs
# hung and leaked a claude.exe process every 30 minutes).
#
# Runs from Windows Task Scheduler ('KL sync-kl-main', every 30 min) via
# run_sync_kl_main.vbs so no console window flashes.

$Repo = "C:\Users\jackd\Documents\KL"
$Log = Join-Path $Repo "run-logs\sync-kl-main.log"
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
git -C $Repo fetch origin 2>&1 | Out-Null
$out = (git -C $Repo merge --ff-only origin/main 2>&1) -join ' | '
"$stamp $out" | Add-Content -Path $Log -Encoding utf8

# One-time bootstrap: the first sync after register_dashboard_task.ps1 lands,
# register the daily 7 AM dashboard rebuild and kick its first run — so the
# task appears on this machine with no manual step. No-op once it exists.
$dashTask = Get-ScheduledTask -TaskName "KL dashboards-daily" -ErrorAction SilentlyContinue
if (-not $dashTask) {
    $reg = Join-Path $Repo "register_dashboard_task.ps1"
    if (Test-Path $reg) {
        $regOut = (& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $reg 2>&1) -join ' | '
        $ok = [bool](Get-ScheduledTask -TaskName "KL dashboards-daily" -ErrorAction SilentlyContinue)
        "$stamp dashboard-task bootstrap: $(if ($ok) { 'registered' } else { 'FAILED (run register_dashboard_task.ps1 from an admin PowerShell)' }) | $regOut" | Add-Content -Path $Log -Encoding utf8
        if ($ok) { Start-ScheduledTask -TaskName "KL dashboards-daily" }
    }
} elseif ($dashTask.State -ne 'Running' -and
          -not (Test-Path (Join-Path $Repo "Kalshi-Settlements-archive.csv")) -and
          ((Test-Path (Join-Path $Repo "seed_settlements_*.csv")) -or
           (Test-Path (Join-Path $Repo "KalshiRecentActivitySettlement*.csv")))) {
    # A seed export was dropped in but the settlements archive hasn't been
    # built yet: run the rebuild now instead of waiting for the 7 AM slot.
    # Self-limiting — the archive exists after the first successful run.
    Start-ScheduledTask -TaskName "KL dashboards-daily"
    "$stamp dashboards kicked to build first settlements archive from seed" | Add-Content -Path $Log -Encoding utf8
} elseif ($dashTask.State -ne 'Running' -and
          (Test-Path (Join-Path $Repo "Kalshi-Settlements-archive.csv")) -and
          -not (Test-Path (Join-Path $Repo "run-logs\dashboards-dedup-v2.kicked"))) {
    # One-time heal (2026-08-31): the first archive build double-counted
    # settlements covered by both the seed export and the API pull (their
    # settled-time strings differ, and the old dedup key included time).
    # The merge now dedups on ticker alone and collapses the duplicates on
    # its next load — kick a rebuild once so the dashboards correct now.
    New-Item -ItemType File -Force (Join-Path $Repo "run-logs\dashboards-dedup-v2.kicked") | Out-Null
    Start-ScheduledTask -TaskName "KL dashboards-daily"
    "$stamp dashboards kicked once to dedup settlements archive (v2 key)" | Add-Content -Path $Log -Encoding utf8
}

# One-shot windowed IMM restart (Jack 2026-08-24 "restart for me at that
# time"): bumping $RestartRequest in a main push makes the NEXT sync run
# dispatch restart_imm.ps1 exactly once on this machine — the script itself
# waits for the :50-:05 hourly-temp dead window before killing the bot, and
# the launcher relaunches it on the freshly synced code. The done-stamp is
# LOCAL (run-logs\incentive-mm\ is untracked), so each request id fires
# once here; a future restart request = push a new id. Dispatched DETACHED
# on purpose: the window wait can be ~45 min and this sync runs every 30 —
# a blocking call would pile syncs up behind it. Note the two-tick latency:
# the sync that PULLS a new id is still executing the old script, so the
# id dispatches on the run after (~30-60 min post-push), then the window.
# Stamp is written AFTER a successful dispatch — a failed Start-Process
# retries next sync (a duplicate windowed restart is harmless; a silently
# lost one is not).
# -2: retry (2026-08-24 pm) — the first id's outcome was unobservable from
# the cloud side and KXTRUEV was still dark after a proven-good dry run;
# ids are cheap. Once the in-bot self-restart (incentive_mm
# code_change_exit_due) is running, future code deploys need no dispatch
# at all — this stays for env changes and belt-and-suspenders.
# 2026-09-02: belt-and-suspenders invoked for real — order-history
# forensics (imm_deploy_probe run 4) proved the bot padded and quoted
# KXMAMDANIMENTION-26SEP01 straight through its live window on 09-01
# (121 pad-priced orders to 14:41Z, 3,270 contracts filled), 34h after
# the live-event depth gate merged: the self-restart never fired, so the
# sync itself is the suspect. If THIS file's new id is executing, the
# sync recovered — dispatch the restart.
$RestartRequest = '20260902-event-depth-gate-deploy'
if ($RestartRequest) {
    $rrDir = Join-Path $Repo "run-logs\incentive-mm"
    $rrStamp = Join-Path $rrDir "restart_request_$RestartRequest.done"
    $rrScript = Join-Path $Repo "restart_imm.ps1"
    if (-not (Test-Path $rrStamp) -and (Test-Path $rrScript)) {
        New-Item -ItemType Directory -Force $rrDir | Out-Null
        $rrProc = Start-Process -PassThru -WindowStyle Hidden powershell.exe -ArgumentList @(
            "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $rrScript)
        if ($rrProc) {
            "$stamp restart request '$RestartRequest' dispatched (restart_imm.ps1 pid $($rrProc.Id), waits for :50-:05 window)" | Add-Content -Path $Log -Encoding utf8
            New-Item -ItemType File -Force $rrStamp | Out-Null
        } else {
            "$stamp ! restart request '$RestartRequest' FAILED to dispatch; will retry next sync" | Add-Content -Path $Log -Encoding utf8
        }
    }
}

# Cap the log at ~500 lines so it never grows unbounded
$lines = @(Get-Content $Log)
if ($lines.Count -gt 500) { $lines[-200..-1] | Set-Content -Path $Log -Encoding utf8 }
