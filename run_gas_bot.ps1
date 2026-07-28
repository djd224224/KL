# Launcher for the AAA gas-price bot (gas_bot.py). Mirrors run_incentive_mm.ps1:
# runs under Windows Task Scheduler, logs to run-logs\gas-bot\, pulls alert
# creds from HKCU, restarts on exit (carry mode only).
#
# NOT REGISTERED AS A TASK YET (deliberate — see GAS_BOT_HANDOFF.md for the
# go-live checklist and Register-ScheduledTask commands).
#
# Modes:
#   -Mode snipe : one pass — wait inside the 02:50-04:40 ET window for the new
#                 AAA print, take stale quotes, exit. Schedule daily ~02:45 ET.
#   -Mode carry : continuous requote loop (restarts forever like the MM bots).
# Dry run unless -Live is passed.

param(
    [ValidateSet("snipe", "carry")] [string]$Mode = "carry",
    [switch]$Live
)

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"

try { Start-Transcript -Path (Join-Path $Repo "run-logs\gas-bot\launcher-transcript.log") -Append | Out-Null } catch {}

Set-Location $Repo
New-Item -ItemType Directory -Force (Join-Path $Repo "run-logs\gas-bot") | Out-Null

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

$LiveArg = if ($Live) { "--live" } else { "" }

while ($true) {
    $log = Join-Path $Repo ("run-logs\gas-bot\gas-bot-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
    Add-Content $log "$(Get-Date -Format u) launcher: starting gas_bot $Mode $(if ($Live) { 'LIVE' } else { 'DRY' })"
    # cmd-level redirection appends raw utf-8 bytes; PowerShell's *>> would
    # write UTF-16 and wrap stderr lines in NativeCommandError noise.
    & cmd.exe /c "set PYTHONPATH=$UserSite&& `"$Python`" `"$Repo\gas_bot.py`" --mode $Mode $LiveArg >> `"$log`" 2>&1"
    Add-Content $log "$(Get-Date -Format u) launcher: bot exited (code $LASTEXITCODE)"
    if ($Mode -eq "snipe") { break }   # one window per task run
    Start-Sleep -Seconds 30
}
