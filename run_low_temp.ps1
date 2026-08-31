# Launcher for the KXLOW low-temperature bot (low_temp_trading.py) under
# Windows Task Scheduler — task 'KL low-temp-trading', 4 daily triggers
# (ET local; machine runs ET, bot logic is CT/city-local internally):
#   20:17 ET (19:17 CT)  evening run  → trades TOMORROW's lows (main)
#   23:17 ET (22:17 CT)  late refresh → trades TOMORROW's lows
#   03:17 ET (02:17 CT)  day-of run   → TODAY's lows, CT/MT/PT cities
#   04:47 ET (03:47 CT)  day-of run   → MT/PT cities
# Single-shot (run-to-completion script, not a resident loop). Safe at any
# hour: cities past their quote stop are skipped in-script.
#
# GH Actions "Low temp trading" keeps workflow_dispatch as a manual backup;
# its cron schedule was removed when this task took over (2026-07-22) so
# the two schedulers can never double-run a slot.
#
# No secrets live in this file: key path + alert credentials come from the
# user-level environment (HKCU\Environment), same as the sibling launchers.
#
# Logs: run-logs\low-temp\low-temp-YYYY-MM-DD.log (appended across the
# day's runs). Nonzero exit emails the log tail via send_alert_email.py.

$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$Repo = "C:\Users\jackd\Documents\KL"

Set-Location $Repo
$LogDir = Join-Path $Repo "run-logs\low-temp"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("low-temp-{0:yyyy-MM-dd}.log" -f (Get-Date))

# Kill switch: while a tracked low_temp.paused file exists in the repo
# (lands here via the 30-min main sync), every trigger is a silent no-op.
# Delete the file from main to resume. low_temp_trading.py checks it too,
# covering the GH workflow_dispatch backup and direct invocations.
if (Test-Path (Join-Path $Repo "low_temp.paused")) {
    "=== $(Get-Date -Format 'u') low-temp run SKIPPED — low_temp.paused present ===" | Add-Content -Path $LogPath -Encoding utf8
    exit 0
}

$env:PYTHONIOENCODING = "utf-8"
# Task Scheduler sessions can lack APPDATA (hides pip --user installs).
# Passed via `set` inside the cmd line below (the incentive_mm launcher's
# proven pattern) — the first TS run of this launcher lost a $env:-set
# PYTHONPATH and died on google.auth. Main site-packages has since been
# made self-sufficient (google-auth installed 2026-07-22), so this is
# belt-and-suspenders.
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
# Testing size (Jack 2026-07-24): 2/rung day-of, 4/rung evening (x2 night)
# — raised from 1/2 (vs 15/30 on the high bot). Keep in sync with the
# script default + workflow. Raise further after real KXLOW P&L.
$env:LOW_STARTING_CONTRACTS = "2"
$env:LOW_MAX_CONTRACTS = "50"
$env:TRADE_TAILS = "false"

# Task Scheduler sessions do not reliably inherit user env vars — pull
# secrets/paths from HKCU (set persistently at user-env level).
foreach ($n in 'KALSHI_PRIVATE_KEY_PATH', 'ALERT_EMAIL_FROM', 'ALERT_EMAIL_PASSWORD', 'ALERT_EMAIL_TO') {
    if (-not (Get-Item "env:$n" -ErrorAction SilentlyContinue)) {
        try { Set-Item "env:$n" (Get-ItemProperty -Path "HKCU:\Environment" -Name $n -ErrorAction Stop).$n } catch {}
    }
}
# Alert emails go to the inbox (tmomail SMS gateway dead since 2026-06-29).
if (-not $env:ALERT_EMAIL_TO) { $env:ALERT_EMAIL_TO = "jackdu224@gmail.com" }

"=== $(Get-Date -Format 'u') low-temp run start ===" | Add-Content -Path $LogPath -Encoding utf8

# cmd-level redirection appends raw utf-8 bytes; PowerShell's *>> would
# write UTF-16 and wrap stderr lines in NativeCommandError noise.
& cmd.exe /c "set PYTHONPATH=$UserSite&& `"$Python`" `"$Repo\low_temp_trading.py`" >> `"$LogPath`" 2>&1"
$ExitCode = $LASTEXITCODE

"=== $(Get-Date -Format 'u') low-temp run exit=$ExitCode ===" | Add-Content -Path $LogPath -Encoding utf8

# Never fail silently: on nonzero exit, email the log tail.
if ($ExitCode -ne 0) {
    $tail = (Get-Content $LogPath -Tail 12) -join "`n"
    & $Python (Join-Path $Repo "send_alert_email.py") `
        "low-temp bot FAILED (exit=$ExitCode)" `
        "A scheduled low_temp_trading.py run failed. Log: $LogPath`n`n$tail" `
        2>&1 | Add-Content -Path $LogPath -Encoding utf8
}

exit $ExitCode
