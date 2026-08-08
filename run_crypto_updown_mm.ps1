# Launcher for the crypto ABOVE/BELOW market maker (crypto_updown_mm.py).
# Sibling of run_crypto_touch_mm.ps1; runs under Windows Task Scheduler (at
# logon), restarts the bot forever if it exits, and logs to
# run-logs\crypto-updown\<asset>-<cadence>-YYYY-MM-DD.log.
# One scheduled task per asset: pass -Asset (and optionally -Cadence/-PollSecs).
#
# These are TERMINAL binaries (settle on the price AT the hour), not one-touch.
# They are priced by a different model in a different module — do not point this
# launcher at crypto_touch_mm.py.
#
# No secrets live in this file: alert credentials are read from the user-level
# environment (HKCU\Environment).

param(
    [string]$Asset = "BTC",
    [string]$Cadence = "both",     # hourly | daily | weekly | both | all
    [string]$PollSecs = "30"
)

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"

Set-Location $Repo
New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\crypto-updown") | Out-Null
# The Kraken/Coinbase data cache is shared with the one-touch fleet on purpose
# (per-IP rate limits); make sure its directory exists even if that fleet is off.
New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\crypto-touch") | Out-Null

# CUD_*, not CMM_*: this bot has its own namespace so it can never be retuned
# by (or retune) the live one-touch tasks.
$env:CUD_POLL_SECS = $PollSecs
$env:PYTHONIOENCODING = "utf-8"
# Task Scheduler sessions can lack APPDATA, which hides pip --user installs.
# Set PYTHONPATH inside the cmd line below — env set here has not reliably
# reached python under the task context.
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
foreach ($v in "ALERT_EMAIL_FROM", "ALERT_EMAIL_PASSWORD") {
    if (-not (Get-Item "env:$v" -ErrorAction SilentlyContinue)) {
        $val = [Environment]::GetEnvironmentVariable($v, "User")
        if ($val) { Set-Item "env:$v" $val }
    }
}

# Spread startups so the fleet doesn't hammer the data APIs simultaneously.
Start-Sleep -Seconds (Get-Random -Maximum 45)

while ($true) {
    $log = Join-Path $Repo ("run-logs\crypto-updown\{0}-{1}-{2}.log" -f $Asset.ToLower(), $Cadence.ToLower(), (Get-Date -Format "yyyy-MM-dd"))
    Add-Content $log "$(Get-Date -Format u) launcher: starting updown $Asset ($Cadence) live"
    # cmd-level redirection appends raw utf-8 bytes; PowerShell's *>> would
    # write UTF-16 and wrap stderr lines in NativeCommandError noise.
    Add-Content $log "launcher env: PYTHONPATH=$UserSite CUD_POLL_SECS=$env:CUD_POLL_SECS"
    & cmd.exe /c "set PYTHONPATH=$UserSite&& set CUD_POLL_SECS=$PollSecs&& `"$Python`" `"$Repo\crypto_updown_mm.py`" --asset $Asset --cadence $Cadence --live >> `"$log`" 2>&1"
    Add-Content $log "$(Get-Date -Format u) launcher: bot exited (code $LASTEXITCODE); restarting in 30s"
    Start-Sleep -Seconds 30
}
