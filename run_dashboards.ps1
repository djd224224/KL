# Daily 7:00 AM rebuild of the two live-fetch dashboards
#   kalshi_dashboard_latest.html   (analyze_kalshi_dashboard.py)
#   weather_dashboard_latest.html  (analyze_weather_dashboard.py)
# so a browser tab left open on either file:/// page shows fresh numbers —
# both pages carry a 15-min meta refresh that re-reads the rebuilt files.
#
# Runs from Windows Task Scheduler ('KL dashboards-daily', registered by
# register_dashboard_task.ps1) via run_dashboards_hidden.vbs so no console
# window flashes. Logs to run-logs\dashboards.log (capped).

$Repo = "C:\Users\jackd\Documents\KL"
Set-Location $Repo
$Log = Join-Path $Repo "run-logs\dashboards.log"
New-Item -ItemType Directory -Force (Split-Path $Log) | Out-Null

function Log-Line($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Add-Content -Path $Log -Encoding utf8
}

# Task Scheduler sessions do not reliably inherit user env vars — pull the
# Kalshi key path from HKCU like run_kalshi_daily_recap.ps1 does. (If unset,
# the fetch scripts fall back to Lisa_Kalshi.txt in the repo root.)
if (-not $env:KALSHI_PRIVATE_KEY_PATH) {
    try {
        $env:KALSHI_PRIVATE_KEY_PATH = (Get-ItemProperty -Path "HKCU:\Environment" -Name "KALSHI_PRIVATE_KEY_PATH" -ErrorAction Stop).KALSHI_PRIVATE_KEY_PATH
    } catch {}
}
$env:PYTHONIOENCODING = "utf-8"

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

# Fixed output names (covered by the Kalshi-*.csv gitignore patterns) so daily
# runs overwrite two files instead of piling up timestamped CSVs.
$env:SETTLEMENTS_OUT = "Kalshi-Settlements-daily.csv"
$env:TRADES_OUT = "Kalshi-Trades-daily.csv"

Log-Line "=== dashboards rebuild start ==="
$failed = @()

function Run-Step($name, $cmdArgs) {
    Log-Line "-- $name"
    & $Python @cmdArgs 2>&1 | Add-Content -Path $Log -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        $script:failed += $name
        Log-Line "!! $name failed (exit=$LASTEXITCODE)"
        return $false
    }
    return $true
}

$settleOk = Run-Step "fetch_settlements" @("fetch_settlements_csv.py")
$tradesOk = Run-Step "fetch_trades" @("fetch_trades_csv.py")

# Kalshi's portfolio endpoints only serve a ~65-day rolling window (see
# bq_retention_guard.py), so every pull — plus any seed exports dropped in
# the repo root — is merged into local append-only archives, and the
# analyzers read the ARCHIVES. History accumulates instead of rolling off.
# (Glob patterns are literal here; merge_kalshi_archive.py expands them.)
$SettlementsArchive = "Kalshi-Settlements-archive.csv"
$TradesArchive = "Kalshi-Trades-archive.csv"
$mergeOk = $false
if ($settleOk) {
    $mergeOk = Run-Step "merge_settlements" @("merge_kalshi_archive.py", $SettlementsArchive,
        $env:SETTLEMENTS_OUT, "seed_settlements_*.csv", "KalshiRecentActivitySettlement*.csv")
}
if ($tradesOk) {
    Run-Step "merge_trades" @("merge_kalshi_archive.py", $TradesArchive, $env:TRADES_OUT, "seed_trades_*.csv") | Out-Null
}

if ($mergeOk) {
    # Trades are optional for both analyzers — a stale trades archive (or
    # 'none' before the first trades merge) still beats skipping the rebuild.
    $trades = if (Test-Path $TradesArchive) { $TradesArchive } else { "none" }
    Run-Step "kalshi_dashboard" @("analyze_kalshi_dashboard.py", $SettlementsArchive, $trades, "none", "Kalshi", "kalshi_dashboard_latest.html") | Out-Null
    Run-Step "weather_dashboard" @("analyze_weather_dashboard.py", $SettlementsArchive, $trades, "weather_dashboard_latest.html") | Out-Null
} else {
    Log-Line "!! skipping both analyzers - settlements fetch/merge failed (yesterday's HTML left in place)"
}

Log-Line "=== dashboards rebuild done (failed: $(if ($failed) { $failed -join ', ' } else { 'none' })) ==="

# Cap the log so it never grows unbounded (fetch pagination is chatty).
$lines = @(Get-Content $Log)
if ($lines.Count -gt 2000) { $lines[-1000..-1] | Set-Content -Path $Log -Encoding utf8 }

# Never fail silently (same ethos as run_kalshi_daily_recap.ps1): on any
# failed step, email the log tail via the ALERT_EMAIL_* env vars.
if ($failed.Count -gt 0) {
    foreach ($n in 'ALERT_EMAIL_FROM','ALERT_EMAIL_PASSWORD','ALERT_EMAIL_TO') {
        if (-not (Get-Item "env:$n" -ErrorAction SilentlyContinue)) {
            try { Set-Item "env:$n" (Get-ItemProperty -Path "HKCU:\Environment" -Name $n -ErrorAction Stop).$n } catch {}
        }
    }
    $tail = (Get-Content $Log -Tail 15) -join "`n"
    & $Python (Join-Path $Repo "send_alert_email.py") `
        "dashboards-daily FAILED ($($failed -join ', '))" `
        "The 7:00 AM dashboard rebuild had failures. Log: $Log`n`n$tail" `
        2>&1 | Add-Content -Path $Log -Encoding utf8
    exit 1
}

exit 0
