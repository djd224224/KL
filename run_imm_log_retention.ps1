# Launcher for the IMM log retention (review R-261124-03; Jack 2026-11-25:
# "Yes, 35 days"). Task Scheduler job "KL imm log-retention", daily 02:40 ET,
# through run_imm_log_retention_hidden.vbs (no console window). The task's
# action carries the job's arguments (register_imm_log_retention.ps1 sets
# them): --apply --not-before 2026-11-27 --max-gb 10.
#
# imm_log_retention.py gzips the bot's text logs older than 35 days in place.
# Each copy is verified byte for byte before the plain file goes, and each
# file gets one manifest line. Nothing is deleted outright. Its own lines go
# to run-logs\incentive-mm\retention\run.log; this launcher's lines and any
# traceback go to task.log there. The exit code passes through to Task
# Scheduler's Last Run Result, which the daily review reads, along with
# last_run.json:
#   0 ok (a dry run or off included), 1 a file failed (its original is kept),
#   2 a .gz conflict (stopped), 3 refused (the readers are not .gz-aware),
#   4 another run holds the lock.
#
#   run_imm_log_retention.ps1                  # a dry run: lists what would compress
#   run_imm_log_retention.ps1 --apply --max-gb 10
#   Stop: IMM_LOG_RETENTION=0, or create run-logs\incentive-mm\retention\STOP
#   Undo: python imm_log_retention.py restore --all   (create STOP first)

$Repo = "C:\Users\jackd\Documents\KL"
$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
$OutDir = Join-Path $Repo "run-logs\incentive-mm\retention"
New-Item -ItemType Directory -Force $OutDir | Out-Null
$TaskLog = Join-Path $OutDir "task.log"

function Log([string]$Line) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: $Line" | Add-Content -Path $TaskLog -Encoding ascii
}

# the job's arguments are flags, dates and numbers: no quoting needed
$argline = ($args | ForEach-Object { [string]$_ }) -join ' '
Log "start: imm_log_retention.py $argline"
Set-Location $Repo
# cmd-level redirection appends the raw bytes (PowerShell 5.1's *>> would
# write UTF-16 into the same file)
& cmd.exe /c "set PYTHONPATH=$UserSite&& set PYTHONIOENCODING=utf-8&& `"$Python`" `"$Repo\imm_log_retention.py`" $argline >> `"$TaskLog`" 2>&1"
$rc = $LASTEXITCODE
Log "exit $rc"
exit $rc
