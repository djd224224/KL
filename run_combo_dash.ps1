# Launcher for the combo maker's READ-ONLY dashboard (combo_dash.py,
# http://127.0.0.1:8772; Jack 2026-11-06: "Yes, add the task" -- the app had
# stopped the dash's preview server when its session went idle). Restarts the
# dash whenever it exits and logs launcher lines (plus everything the dash
# prints, e.g. a traceback) to run-logs\combo-dash\launcher-YYYY-MM-DD.log.
#
# READ-ONLY: it reads the maker's files and makes a few Kalshi GETs (balance,
# fills, markets, candles); it never places, quotes, amends or cancels
# anything. It binds 127.0.0.1 only.
#
# Started by the "KL combo-dash" task (register_combo_dash.ps1) through
# run_combo_dash_hidden.vbs (no console window).
#
# Exit codes from the dash:
#   3  run-logs\combo-dash\combo_dash.stop exists        -> stop looping
#   4  port 8772 is taken (another dashboard serves it)  -> stop looping
#      (that one runs on; the task's 10-min trigger checks again)
#   anything else -> restart after 20 s
#
# Stop it: create run-logs\combo-dash\combo_dash.stop (the dash exits within
# ~2 s and this loop ends; the task's retries exit at once while the file
# exists). Delete the file to let the next 10-min trigger restart it.
# For good: also Unregister-ScheduledTask -TaskName 'KL combo-dash' -Confirm:$false

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$DataDir = if ($env:COMBO_DASH_DIR) { $env:COMBO_DASH_DIR } else { Join-Path $Repo "run-logs\combo-dash" }

Set-Location $Repo
New-Item -ItemType Directory -Force $DataDir | Out-Null
$env:PYTHONIOENCODING = "utf-8"
if (-not $env:PYTHONPATH) { $env:PYTHONPATH = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages" }
# Windows PowerShell 5.1: make the *>> redirection below write UTF-8 like Add-Content
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

while ($true) {
    $log = Join-Path $DataDir ("launcher-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: starting combo_dash.py" | Add-Content -Path $log -Encoding utf8
    & $Python "$Repo\combo_dash.py" --port 8772 *>> $log
    $code = $LASTEXITCODE
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: dash exited with $code" | Add-Content -Path $log -Encoding utf8
    if ($code -eq 3) {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: stop file present; not restarting" | Add-Content -Path $log -Encoding utf8
        exit 3
    }
    if ($code -eq 4) {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: port 8772 is taken; this launcher exits" | Add-Content -Path $log -Encoding utf8
        exit 4
    }
    Start-Sleep -Seconds 20
}
