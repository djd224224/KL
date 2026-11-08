# Launcher for the follow probe (follow_probe.py): 1-contract orders at the
# theoretical price on wide incentive-reward books, to see whether other makers
# follow. Restarts the bot if it exits and logs its output to
# run-logs\follow-probe\launcher-YYYY-MM-DD.log (the bot keeps its own
# probe_<date>.log, trials / timeline / orders / fills jsonl beside it).
#
# DRY RUN by default: nothing is placed (the no-order baseline).
# -Live places real 1-contract orders in the numbered Kalshi subaccount given
# by -Subaccount; create and fund it first with kalshi_subaccount.py
# (python follow_probe.py --setup prints the steps).
#
# NOT REGISTERED AS A SCHEDULED TASK (deliberate): Jack starts it. Detached:
#   Start-Process powershell -WindowStyle Hidden -ArgumentList '-ExecutionPolicy','Bypass','-File','C:\Users\jackd\Documents\KL\run_follow_probe.ps1','-Live','-Subaccount','N'
# -MaxOpen M runs M probes at once (FPROBE_MAX_OPEN; default 6; one per event;
# started 4 per 5 s). A restart keeps today's caps and the markets already used.
# Stop: create run-logs\follow-probe\HALT (the bot cancels its orders and
# idles while it exists), or end the powershell / python processes.

param(
    [switch]$Live,
    [int]$Subaccount = 0,
    [int]$MaxOpen = 0
)

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$LogDir = Join-Path $Repo "run-logs\follow-probe"

if ($Live -and $Subaccount -le 0) {
    Write-Host "-Live needs -Subaccount N (the probe trades its own subaccount; see kalshi_subaccount.py)"
    exit 1
}

Set-Location $Repo
New-Item -ItemType Directory -Force $LogDir | Out-Null
$env:PYTHONIOENCODING = "utf-8"
if ($MaxOpen -gt 0) { $env:FPROBE_MAX_OPEN = "$MaxOpen" }

$BotArgs = @("$Repo\follow_probe.py", "--quiet")
if ($Live) { $BotArgs += "--live" }
if ($Subaccount -gt 0) { $BotArgs += @("--subaccount", "$Subaccount") }
$Mode = if ($Live) { "live, subaccount $Subaccount" } else { "dry run (no-order baseline)" }
if ($MaxOpen -gt 0) { $Mode += ", $MaxOpen at once" }

while ($true) {
    $log = Join-Path $LogDir ("launcher-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: starting ($Mode)" | Add-Content -Path $log -Encoding utf8
    & $Python @BotArgs *>> $log
    $code = $LASTEXITCODE
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: exited with $code" | Add-Content -Path $log -Encoding utf8
    if ($code -eq 2 -or $code -eq 3) {
        # 2 = refused to start (unfunded / shared subaccount, another instance); 3 = halted
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: not restarting (see probe_<date>.log)" | Add-Content -Path $log -Encoding utf8
        exit $code
    }
    Start-Sleep -Seconds 30
}
