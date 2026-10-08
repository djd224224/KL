# Launcher for the NFL prop sniper (nfl_snipe_bot.py), a taker bot separate
# from the IMM. Restarts the bot if it exits and logs the bot's output to
# run-logs\nfl-snipe\launcher-YYYY-MM-DD.log (the bot keeps its own
# snipe_<date>.log, orders / fills jsonl and books beside it).
#
# DRY RUN by default. -Live trades, in the numbered Kalshi subaccount given by
# -Subaccount (Jack 2026-10-08: "yes subaccount, $250 at risk"): create and
# fund it first with kalshi_subaccount.py. $250 at risk is the bot's default
# total; -MaxRiskTotal overrides it.
#
# NOT REGISTERED AS A SCHEDULED TASK (deliberate, like the Polymarket bot):
# Jack starts it. Start it detached, e.g.
#   Start-Process powershell -WindowStyle Hidden -ArgumentList '-ExecutionPolicy','Bypass','-File','C:\Users\jackd\Documents\KL\run_nfl_snipe.ps1','-Live','-Subaccount','1'
# Stop: create run-logs\nfl-snipe\HALT (the bot idles while it exists), or end
# the powershell / python processes running run_nfl_snipe.ps1 / nfl_snipe_bot.py.

param(
    [switch]$Live,
    [int]$Subaccount = 0,
    [double]$MaxRiskTotal = 0
)

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$LogDir = Join-Path $Repo "run-logs\nfl-snipe"

if ($Live -and $Subaccount -le 0) {
    Write-Host "-Live needs -Subaccount N (the bot trades its own subaccount; see kalshi_subaccount.py)"
    exit 1
}

Set-Location $Repo
New-Item -ItemType Directory -Force $LogDir | Out-Null
$env:PYTHONIOENCODING = "utf-8"

$BotArgs = @("$Repo\nfl_snipe_bot.py")
if ($Live) { $BotArgs += "--live" }
if ($Subaccount -gt 0) { $BotArgs += @("--subaccount", "$Subaccount") }
if ($MaxRiskTotal -gt 0) { $BotArgs += @("--max-risk-total", "$MaxRiskTotal") }
$Mode = if ($Live) { "live, subaccount $Subaccount" } else { "dry run" }

while ($true) {
    $log = Join-Path $LogDir ("launcher-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: starting ($Mode)" | Add-Content -Path $log -Encoding utf8
    & $Python @BotArgs *>> $log
    $code = $LASTEXITCODE
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: exited with $code" | Add-Content -Path $log -Encoding utf8
    if ($code -eq 2) {
        # 2 = refused to start (unfunded subaccount, unreadable balance): a restart would not help
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: not restarting (see above)" | Add-Content -Path $log -Encoding utf8
        exit 2
    }
    Start-Sleep -Seconds 30
}
