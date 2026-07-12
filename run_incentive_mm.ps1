# Launcher for the Kalshi incentive-rewards market maker (incentive_mm.py).
# Mirrors run_crypto_touch_mm.ps1: runs under Windows Task Scheduler (at
# logon), restarts the bot forever if it exits, logs to
# run-logs\incentive-mm\incentive-mm-YYYY-MM-DD.log.
#
# NOT REGISTERED AS A TASK YET (deliberate — see INCENTIVE_MM_HANDOFF.md for
# the go-live checklist and the Register-ScheduledTask command).
#
# No secrets live in this file: alert credentials are read from the
# user-level environment (HKCU\Environment).

param(
    [string]$PollSecs = "90",
    # Micro-probe mode (go-live phase 1): 1/5-size ladders on ~10 markets,
    # ~$200 collateral. Run one full PAID period this way and reconcile
    # Kalshi's actual credits against the estimator before scaling up.
    [switch]$Probe
)

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"

Set-Location $Repo
New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\incentive-mm") | Out-Null

$env:PYTHONIOENCODING = "utf-8"
# Task Scheduler sessions can lack APPDATA (hides pip --user installs).
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
# The task session may not inherit user env vars — pull alert creds from HKCU.
foreach ($v in "ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD") {
    if (-not (Get-Item "env:$v" -ErrorAction SilentlyContinue)) {
        $val = [Environment]::GetEnvironmentVariable($v, "User")
        if ($val) { Set-Item "env:$v" $val }
    }
}

# Spread startup vs the crypto fleet's at-logon herd (shared account API).
Start-Sleep -Seconds (Get-Random -Maximum 45)

$ProbeEnv = ""
if ($Probe) {
    $ProbeEnv = "set IMM_LEVELS=0:1,1:2,2:4&& set IMM_MAX_MARKETS=10&& set IMM_COLLATERAL_BUDGET=200&& "
}

while ($true) {
    $log = Join-Path $Repo ("run-logs\incentive-mm\incentive-mm-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    Add-Content $log "$(Get-Date -Format u) launcher: starting incentive_mm live$(if ($Probe) { ' [MICRO-PROBE]' })"
    # cmd-level redirection appends raw utf-8 bytes; PowerShell's *>> would
    # write UTF-16 and wrap stderr lines in NativeCommandError noise.
    & cmd.exe /c "set PYTHONPATH=$UserSite&& set IMM_POLL_SECS=$PollSecs&& $ProbeEnv`"$Python`" `"$Repo\incentive_mm.py`" --live >> `"$log`" 2>&1"
    Add-Content $log "$(Get-Date -Format u) launcher: bot exited (code $LASTEXITCODE); restarting in 30s"
    Start-Sleep -Seconds 30
}
