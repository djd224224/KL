# Launcher for the crypto one-touch market maker (crypto_touch_mm.py).
# Runs under Windows Task Scheduler (at logon); restarts the bot forever if it
# exits, logging to run-logs\crypto-touch\<market>-YYYY-MM-DD.log.
# One scheduled task per market: pass -Market (and optionally -PollSecs).
#
# No secrets live in this file: alert credentials are read from the user-level
# environment (HKCU\Environment) — set via
# [Environment]::SetEnvironmentVariable(..,'User').

param(
    [string]$Market = "SOL-MAX",
    [string]$PollSecs = "15"
)

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"

Set-Location $Repo
New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\crypto-touch") | Out-Null

$env:CMM_POLL_SECS = $PollSecs
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

# Spread fleet startups (esp. at-logon, when all 16 tasks fire at once) so the
# bots don't hammer the data APIs simultaneously — Kraken temp-bans the IP.
Start-Sleep -Seconds (Get-Random -Maximum 45)

while ($true) {
    $log = Join-Path $Repo ("run-logs\crypto-touch\{0}-{1}.log" -f $Market.ToLower(), (Get-Date -Format "yyyy-MM-dd"))
    Add-Content $log "$(Get-Date -Format u) launcher: starting $Market live"
    # cmd-level redirection appends raw utf-8 bytes; PowerShell's *>> would
    # write UTF-16 and wrap stderr lines in NativeCommandError noise.
    Add-Content $log "launcher env: PYTHONPATH=$UserSite CMM_POLL_SECS=$env:CMM_POLL_SECS"
    & cmd.exe /c "set PYTHONPATH=$UserSite&& set CMM_POLL_SECS=$PollSecs&& `"$Python`" `"$Repo\crypto_touch_mm.py`" --market $Market --live >> `"$log`" 2>&1"
    Add-Content $log "$(Get-Date -Format u) launcher: bot exited (code $LASTEXITCODE); restarting in 30s"
    Start-Sleep -Seconds 30
}
