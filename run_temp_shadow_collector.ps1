# Launcher for the hourly-temp shadow test's recorder (temp_shadow_collector.py,
# TEMP_SHADOW.md). Restarts the collector whenever it exits and logs launcher
# lines (plus anything the collector prints, e.g. a traceback) to
# KL-data\temp-shadow\launcher-YYYY-MM-DD.log; the collector keeps its own
# collector.log and collector_status.json beside it.
#
# READ-ONLY recorder: it never places, amends or cancels an order.
#
# Started by the "KL temp-shadow collector" task (register_temp_shadow_collector.ps1)
# through run_temp_shadow_collector_hidden.vbs (no console window).
#
# Exit codes from the collector:
#   3  past SHADOW_UNTIL_UTC or KL-data\temp-shadow\HALT exists -> stop looping
#   4  another live collector holds collector.lock            -> stop looping
#      (that one's own launcher restarts it)
#   anything else -> restart after 20 s
#
# Stop the test: create KL-data\temp-shadow\HALT (the collector exits within a
# second and this loop ends), then
#   Unregister-ScheduledTask -TaskName 'KL temp-shadow collector' -Confirm:$false

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$DataDir = if ($env:TEMP_SHADOW_DATA) { $env:TEMP_SHADOW_DATA } else { "C:\Users\jackd\Documents\KL-data\temp-shadow" }

Set-Location $Repo
New-Item -ItemType Directory -Force $DataDir | Out-Null
$env:PYTHONIOENCODING = "utf-8"
if (-not $env:PYTHONPATH) { $env:PYTHONPATH = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages" }
# Windows PowerShell 5.1: make the *>> redirection below write UTF-8 like Add-Content
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

while ($true) {
    $log = Join-Path $DataDir ("launcher-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: starting temp_shadow_collector.py" | Add-Content -Path $log -Encoding utf8
    & $Python "$Repo\temp_shadow_collector.py" *>> $log
    $code = $LASTEXITCODE
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: collector exited with $code" | Add-Content -Path $log -Encoding utf8
    if ($code -eq 3) {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: past SHADOW_UNTIL or HALT present; not restarting" | Add-Content -Path $log -Encoding utf8
        exit 3
    }
    if ($code -eq 4) {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: another collector holds collector.lock; this launcher exits" | Add-Content -Path $log -Encoding utf8
        exit 4
    }
    Start-Sleep -Seconds 20
}
