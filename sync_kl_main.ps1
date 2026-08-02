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

# Cap the log at ~500 lines so it never grows unbounded
$lines = @(Get-Content $Log)
if ($lines.Count -gt 500) { $lines[-200..-1] | Set-Content -Path $Log -Encoding utf8 }
