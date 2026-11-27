# Launcher for the nights pilot's morning email (Jack 2026-11-26: "give
# email daily report", for MLB next team; 2026-11-27: "Yes, extend it", to
# every series in the night family). Task Scheduler job
# "KL imm mlb-nights", daily 07:15 ET through 2026-12-03, through
# run_mlb_nights_report_hidden.vbs (no console window).
#
# send_mlb_nights_report.py reads the bot's sinks (run config, fills, cycle
# log, selection snapshot, guard sink, bot state) read-only, takes the
# family's members from the bot's own run config, prices the fills at
# Kalshi's last trade, and emails the night (00:00-07:00 ET) and the total
# since each series' first night, series by series. One email a day (a sent-marker in
# run-logs\incentive-mm\mlb_nights\); its lines and any traceback go to
# task.log there. The exit code passes through to Task Scheduler's Last
# Run Result: 0 sent / nothing to send / already sent, 1 the send failed.
#
#   run_mlb_nights_report.ps1                 # today's night, emailed once
#   run_mlb_nights_report.ps1 --dry           # build and print, no email
#   run_mlb_nights_report.ps1 --day 2026-11-27 --test

$Repo = "C:\Users\jackd\Documents\KL"
$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
$OutDir = Join-Path $Repo "run-logs\incentive-mm\mlb_nights"
New-Item -ItemType Directory -Force $OutDir | Out-Null
$TaskLog = Join-Path $OutDir "task.log"

function Log([string]$Line) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: $Line" | Add-Content -Path $TaskLog -Encoding ascii
}

# the arguments are flags and a date: no quoting needed
$argline = ($args | ForEach-Object { [string]$_ }) -join ' '
Log "start: send_mlb_nights_report.py $argline"
Set-Location $Repo
# cmd-level redirection appends the raw bytes (PowerShell 5.1's *>> would
# write UTF-16 into the same file); utf-8 output, since the task's console
# is cp1252
& cmd.exe /c "set PYTHONPATH=$UserSite&& set PYTHONIOENCODING=utf-8&& `"$Python`" `"$Repo\send_mlb_nights_report.py`" $argline >> `"$TaskLog`" 2>&1"
$rc = $LASTEXITCODE
Log "exit $rc"
exit $rc
