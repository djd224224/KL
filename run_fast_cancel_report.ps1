# Launcher for the one-time report on the IMM's faster fast cancel (Jack
# 2026-10-17: "Yes, and email report in 3 days"), Task Scheduler "KL
# fast-cancel-report", once at 5:30 PM ET Tue 2026-10-20, through
# run_fast_cancel_report_hidden.vbs so no console window shows.
#
# Plain python, read-only (no claude -p: the report is arithmetic on the
# bot's own logs):
#  1. imm_fast_cancel_report.py writes
#     run-logs\incentive-mm\fast_cancel_report_<ET date>.md and prints its path.
#  2. md_report_email.py renders it as a page and send_html_email.py emails it
#     (the ALERT_EMAIL_* account, as every KL report). A failure emails the
#     log tail instead.
#  3. A run that emailed the report unregisters the task: it is one-shot.
#
#   run_fast_cancel_report.ps1             # the scheduled run
#   run_fast_cancel_report.ps1 -NoEmail    # a test: report + page, no email, task kept
#
# Logs to run-logs\fast-cancel-report\YYYY-MM-DD.log (the day of the run).

param(
    [switch]$NoEmail
)

$Repo = "C:\Users\jackd\Documents\KL"
$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
$TaskName = "KL fast-cancel-report"
$Subject = "IMM faster fast cancel: 3-day report"

$LogDir = Join-Path $Repo "run-logs\fast-cancel-report"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("{0:yyyy-MM-dd}.log" -f (Get-Date))
function Log([string]$Line) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Line" | Add-Content -Path $LogPath -Encoding utf8
}

# Task Scheduler sessions do not reliably inherit user env vars (see
# run_cpi_pilot_report.ps1): take the mail account from HKCU.
foreach ($n in 'ALERT_EMAIL_FROM','ALERT_EMAIL_PASSWORD','ALERT_EMAIL_TO') {
    if (-not (Get-Item "env:$n" -ErrorAction SilentlyContinue)) {
        try { Set-Item "env:$n" (Get-ItemProperty -Path "HKCU:\Environment" -Name $n -ErrorAction Stop).$n } catch {}
    }
}
if ((Test-Path $UserSite) -and (($env:PYTHONPATH -split ';') -notcontains $UserSite)) {
    $env:PYTHONPATH = (@($UserSite) + @($env:PYTHONPATH -split ';' | Where-Object { $_ })) -join ';'
}
$env:PYTHONIOENCODING = "utf-8"
Set-Location $Repo

function Send-Failure([string]$Why) {
    Log "FAILED: $Why"
    if ($NoEmail) { return }
    $tail = (Get-Content -Path $LogPath -Tail 40 -ErrorAction SilentlyContinue) -join "`n"
    $page = Join-Path $LogDir "failure.html"
    $esc = [System.Net.WebUtility]::HtmlEncode("$Why`n`n$tail")
    "<html><body><pre>$esc</pre></body></html>" | Set-Content -Path $page -Encoding utf8
    & $Python (Join-Path $Repo "send_html_email.py") "$Subject FAILED" $page 2>&1 |
        ForEach-Object { Log "email: $_" }
}

Log "start (NoEmail=$NoEmail)"
$out = @(& $Python (Join-Path $Repo "imm_fast_cancel_report.py") 2>&1 | ForEach-Object { "$_" })
$code = $LASTEXITCODE
$out | ForEach-Object { Log "report: $_" }
$md = $out | Where-Object { $_ -match 'fast_cancel_report_.*\.md$' } | Select-Object -Last 1
if ($code -ne 0 -or -not $md -or -not (Test-Path $md)) {
    Send-Failure "imm_fast_cancel_report.py exited $code without a report"
    exit 1
}
$html = [System.IO.Path]::ChangeExtension($md, ".html")
& $Python (Join-Path $Repo "md_report_email.py") $html $Subject $md 2>&1 |
    ForEach-Object { Log "render: $_" }
if ($LASTEXITCODE -ne 0 -or -not (Test-Path $html)) {
    Send-Failure "md_report_email.py could not render $md"
    exit 1
}
if ($NoEmail) {
    Log "NoEmail: report $md, page $html"
    exit 0
}
& $Python (Join-Path $Repo "send_html_email.py") $Subject $html 2>&1 |
    ForEach-Object { Log "email: $_" }
if ($LASTEXITCODE -ne 0) {
    Log "email FAILED (exit $LASTEXITCODE); the task stays registered"
    exit 1
}
Log "emailed $Subject"
try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Log "task '$TaskName' unregistered (one-shot)"
} catch {
    Log "unregister failed: $_"
}
exit 0
