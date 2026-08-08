# Launcher for the crypto WEEKLY one-touch market maker
# (crypto_touch_mm_weekly.py). Sibling of run_crypto_touch_mm.ps1; runs under
# Windows Task Scheduler (at logon), restarts the bot forever if it exits, and
# logs to run-logs\crypto-touch-weekly\<market>-YYYY-MM-DD.log.
# One scheduled task per market: pass -Market (and optionally -PollSecs).
#
# NOTE: as of 2026-08-05 Kalshi lists no open weekly crypto one-touch events,
# so a bot started here will log "no open weekly event" and idle (placing
# nothing) until one appears. Check with:
#     python crypto_touch_mm_weekly.py --discover
#
# No secrets live in this file: alert credentials are read from the user-level
# environment (HKCU\Environment) — set via
# [Environment]::SetEnvironmentVariable(..,'User').

param(
    [string]$Market = "BTC-MAX",
    [string]$PollSecs = "30"
)

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"

Set-Location $Repo
New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\crypto-touch-weekly") | Out-Null
# The Kraken/Coinbase data cache is shared with the monthly fleet on purpose
# (per-IP rate limits); make sure its directory exists even if that fleet is
# not running.
New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\crypto-touch") | Out-Null

# CTW_*, not CMM_*: the weekly bot has its own namespace so it can never be
# retuned by (or retune) the 16 live monthly tasks.
$env:CTW_POLL_SECS = $PollSecs
$env:PYTHONIOENCODING = "utf-8"
# Task Scheduler sessions can lack APPDATA, which hides pip --user installs
# (requests/pytz/cryptography live in the user site-packages). Set PYTHONPATH
# inside the cmd line below — env set here has not reliably reached python
# under the task context.
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
# The task session may not inherit user env vars — pull alert creds from HKCU.
foreach ($v in "ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD") {
    if (-not (Get-Item "env:$v" -ErrorAction SilentlyContinue)) {
        $val = [Environment]::GetEnvironmentVariable($v, "User")
        if ($val) { Set-Item "env:$v" $val }
    }
}

# Spread startups so the fleet doesn't hammer the data APIs simultaneously —
# Kraken temp-bans the IP.
Start-Sleep -Seconds (Get-Random -Maximum 45)

while ($true) {
    $log = Join-Path $Repo ("run-logs\crypto-touch-weekly\{0}-{1}.log" -f $Market.ToLower(), (Get-Date -Format "yyyy-MM-dd"))
    Add-Content $log "$(Get-Date -Format u) launcher: starting weekly $Market live"
    # cmd-level redirection appends raw utf-8 bytes; PowerShell's *>> would
    # write UTF-16 and wrap stderr lines in NativeCommandError noise.
    Add-Content $log "launcher env: PYTHONPATH=$UserSite CTW_POLL_SECS=$env:CTW_POLL_SECS"
    & cmd.exe /c "set PYTHONPATH=$UserSite&& set CTW_POLL_SECS=$PollSecs&& `"$Python`" `"$Repo\crypto_touch_mm_weekly.py`" --market $Market --live >> `"$log`" 2>&1"
    Add-Content $log "$(Get-Date -Format u) launcher: bot exited (code $LASTEXITCODE); restarting in 30s"
    Start-Sleep -Seconds 30
}
