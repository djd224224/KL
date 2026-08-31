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
    # One-time heal (2026-08-22): the first archive build double-counted
    # settlements covered by both the seed export and the API pull (their
    # settled-time strings differ, and the old dedup key included time).
    # The merge now dedups on ticker alone and collapses the duplicates on
    # its next load — kick a rebuild once so the dashboards correct now.
    New-Item -ItemType File -Force (Join-Path $Repo "run-logs\dashboards-dedup-v2.kicked") | Out-Null
    Start-ScheduledTask -TaskName "KL dashboards-daily"
    "$stamp dashboards kicked once to dedup settlements archive (v2 key)" | Add-Content -Path $Log -Encoding utf8
}

# Cap the log at ~500 lines so it never grows unbounded
$lines = @(Get-Content $Log)
if ($lines.Count -gt 500) { $lines[-200..-1] | Set-Content -Path $Log -Encoding utf8 }
