# Exports everything the new bot laptop needs from THIS (old) machine.
# See MIGRATION_RUNBOOK.md. Two modes:
#   .\migrate_export.ps1            -> full bundle: task XMLs + env values + PEM
#   .\migrate_export.ps1 -StateOnly -> just the runtime state (run at CUTOVER,
#                                      only after the old fleet is stopped)
# Output: .\migration_export\  (git-ignored — CONTAINS LIVE CREDENTIALS.
# Move it by USB, never email it, delete all copies when the migration is done.)
param([switch]$StateOnly)

$repo = "C:\Users\jackd\Documents\KL"
$out = Join-Path $repo "migration_export"
New-Item -ItemType Directory -Force $out | Out-Null

if (-not $StateOnly) {
    # --- scheduled task definitions ---
    $taskDir = Join-Path $out "tasks"
    New-Item -ItemType Directory -Force $taskDir | Out-Null
    $tasks = Get-ScheduledTask -TaskName "KL *" -ErrorAction SilentlyContinue
    foreach ($t in $tasks) {
        $xml = Export-ScheduledTask -TaskName $t.TaskName
        $safe = ($t.TaskName -replace '[^\w\- ]', '_')
        Set-Content -Path (Join-Path $taskDir "$safe.xml") -Value $xml -Encoding Unicode
    }
    "exported $(@($tasks).Count) task XMLs -> $taskDir"

    # --- user-level env vars the launchers/jobs read ---
    $envFile = Join-Path $out "env_values.txt"
    "ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD", "CLOUDSDK_PYTHON" | ForEach-Object {
        "$_=$([Environment]::GetEnvironmentVariable($_, 'User'))"
    } | Set-Content $envFile
    "wrote env values -> $envFile"

    # --- Kalshi private key (hardcoded fallback path in every bot) ---
    $pem = "C:\Users\jackd\Downloads\Lisa_Kalshi.txt"
    if (Test-Path $pem) { Copy-Item $pem (Join-Path $out "Lisa_Kalshi.txt"); "copied PEM" }
    else { Write-Warning "PEM not found at $pem - locate it manually" }
}

# --- runtime state (CUTOVER time only: IMM must be STOPPED or this snapshot
# is stale the moment it lands) ---
$stateDir = Join-Path $out "state"
New-Item -ItemType Directory -Force (Join-Path $stateDir "run-logs\incentive-mm") | Out-Null
New-Item -ItemType Directory -Force (Join-Path $stateDir "gas_data") | Out-Null
$immPatterns = @("*.json", "*.jsonl", "cycle_log_*.csv")
foreach ($p in $immPatterns) {
    Copy-Item (Join-Path $repo "run-logs\incentive-mm\$p") (Join-Path $stateDir "run-logs\incentive-mm\") -ErrorAction SilentlyContinue
}
Copy-Item (Join-Path $repo "gas_data\*") (Join-Path $stateDir "gas_data\") -ErrorAction SilentlyContinue
"state snapshot -> $stateDir  (IMM json/jsonl/cycle logs + gas_data incl. HALT)"
if ($StateOnly) {
    $alive = @(Get-Process python -ErrorAction SilentlyContinue).Count
    if ($alive -gt 0) { Write-Warning "$alive python processes still running - this state snapshot may be STALE. Stop the fleet first (Phase 5 step 1)." }
}
"done. bundle: $out"
