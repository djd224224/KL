# Launcher for the combo maker shadow's READ-ONLY RFQ logger
# (combo_rfq_logger.py; Jack 2026-11-05: "Register the logger as an always-on
# Windows task so it captures every night"). Restarts the logger whenever it
# exits and logs launcher lines (plus everything the logger prints, e.g. a
# traceback) to run-logs\combo-rfq\launcher-YYYY-MM-DD.log; the logger writes
# its own combo_<UTC date>.jsonl beside it.
#
# READ-ONLY: it subscribes to two public WebSocket channels; it never places,
# quotes, amends or cancels anything.
#
# Started by the "KL combo-rfq logger" task (register_combo_rfq_logger.ps1)
# through run_combo_rfq_logger_hidden.vbs (no console window).
#
# Exit codes from the logger:
#   3  run-logs\combo-rfq\combo_rfq_logger.stop exists -> stop looping
#   4  another logger holds combo_rfq_logger.lock      -> stop looping
#      (that one runs on; the task's 10-min trigger checks again)
#   anything else -> restart after 20 s
#
# Stop it: create run-logs\combo-rfq\combo_rfq_logger.stop (the logger exits
# within a second and this loop ends; the task's retries exit at once while
# the file exists). Delete the file to let the next 10-min trigger restart it.
# For good: also Unregister-ScheduledTask -TaskName 'KL combo-rfq logger' -Confirm:$false

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$DataDir = if ($env:COMBO_RFQ_DIR) { $env:COMBO_RFQ_DIR } else { Join-Path $Repo "run-logs\combo-rfq" }

Set-Location $Repo
New-Item -ItemType Directory -Force $DataDir | Out-Null
$env:PYTHONIOENCODING = "utf-8"
if (-not $env:PYTHONPATH) { $env:PYTHONPATH = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages" }
# Windows PowerShell 5.1: make the *>> redirection below write UTF-8 like Add-Content
$PSDefaultParameterValues['Out-File:Encoding'] = 'utf8'

while ($true) {
    $log = Join-Path $DataDir ("launcher-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: starting combo_rfq_logger.py" | Add-Content -Path $log -Encoding utf8
    & $Python "$Repo\combo_rfq_logger.py" --out $DataDir *>> $log
    $code = $LASTEXITCODE
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: logger exited with $code" | Add-Content -Path $log -Encoding utf8
    if ($code -eq 3) {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: stop file present; not restarting" | Add-Content -Path $log -Encoding utf8
        exit 3
    }
    if ($code -eq 4) {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: another logger holds the lock; this launcher exits" | Add-Content -Path $log -Encoding utf8
        exit 4
    }
    Start-Sleep -Seconds 20
}
