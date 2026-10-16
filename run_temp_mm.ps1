# Launcher for the hourly-temp market maker (temp_mm_live.py, KXTEMPMIAH).
# Restarts the bot 20 s after it exits and logs its output to
# KL-data\temp-mm\launcher-YYYY-MM-DD.log (the bot keeps its own tmm_<day>.log,
# orders / fills / decisions jsonl and status.json in KL-data\temp-mm, or in
# KL-data\temp-mm\dry for a dry run).
#
# DRY RUN by default. -Live trades, in the numbered Kalshi subaccount given by
# -Subaccount (never the IMM's primary): create and fund it first with
# kalshi_subaccount.py. -Variant picks the policy: F (default), G, J, or any
# temp_mm_policy.VARIANTS key. Every limit is an env var TMM_<FIELD>
# (TEMP_MM_LIVE.md).
#
# NOT REGISTERED AS A SCHEDULED TASK (deliberate, like the NFL sniper): Jack
# starts it. Start it detached, e.g.
#   Start-Process powershell -WindowStyle Hidden -ArgumentList '-ExecutionPolicy','Bypass','-File','C:\Users\jackd\Documents\KL\run_temp_mm.ps1','-Live','-Subaccount','2','-Variant','F'
# Stop: create KL-data\temp-mm\HALT (the bot cancels every order and idles), or
# end the powershell / python processes running run_temp_mm.ps1 /
# temp_mm_live.py (Ctrl+C / a normal exit also cancels every tmm- order).
#
# Exit codes: 2 = refused to start (no subaccount, $0 cash, lock held, bad
# config, the fair model on built-in defaults) and 3 = halted (daily loss,
# subaccount mismatch, an unconfirmable order, a position our fills do not
# explain, cancels not landing, 40 order rejects) stop the loop; anything else
# restarts after 20 s. Kalshi maintenance, 429s, 5xx and Wi-Fi drops do not
# halt: the bot backs off and resumes by itself.
# Emergency stop while it runs: temp_mm_live.py --live --subaccount N
# --cancel-all (writes HALT, takes no lock).

param(
    [switch]$Live,
    [int]$Subaccount = 0,
    [string]$Variant = "F"
)

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$DataDir = "C:\Users\jackd\Documents\KL-data\temp-mm"

if ($Live -and $Subaccount -le 0) {
    Write-Host "-Live needs -Subaccount N (the bot trades its own subaccount; see kalshi_subaccount.py)"
    exit 2
}

Set-Location $Repo
New-Item -ItemType Directory -Force $DataDir | Out-Null
$env:PYTHONIOENCODING = "utf-8"

$BotArgs = @("$Repo\temp_mm_live.py", "--variant", $Variant, "--quiet")
if ($Live) { $BotArgs += "--live" }
if ($Subaccount -gt 0) { $BotArgs += @("--subaccount", "$Subaccount") }
$Mode = if ($Live) { "live, subaccount $Subaccount, variant $Variant" } else { "dry run, variant $Variant" }

while ($true) {
    $log = Join-Path $DataDir ("launcher-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: starting ($Mode)" | Add-Content -Path $log -Encoding utf8
    & $Python @BotArgs *>> $log
    $code = $LASTEXITCODE
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: exited with $code" | Add-Content -Path $log -Encoding utf8
    if ($code -eq 2 -or $code -eq 3) {
        # 2 = refused to start, 3 = halted (HALT written): a restart would not help
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') launcher: not restarting (exit $code; see tmm_<day>.log)" | Add-Content -Path $log -Encoding utf8
        exit $code
    }
    Start-Sleep -Seconds 20
}
