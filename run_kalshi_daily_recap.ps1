# Launcher for the kalshi-daily-recap Claude Code skill under Windows Task
# Scheduler. Fires claude.exe -p in non-interactive mode so it can wake the
# PC at 8:30 AM, run the recap workflow, and let the machine go back to sleep.
#
# Logs to run-logs\kalshi-daily-recap\YYYY-MM-DD.log.

$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
$Repo = "C:\Users\jackd\Documents\KL"

Set-Location $Repo
$LogDir = Join-Path $Repo "run-logs\kalshi-daily-recap"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("{0:yyyy-MM-dd}.log" -f (Get-Date))

# Task Scheduler sessions do not reliably inherit user env vars — pull the
# Kalshi private key path from HKCU (set persistently at user-env level).
if (-not $env:KALSHI_PRIVATE_KEY_PATH) {
    try {
        $env:KALSHI_PRIVATE_KEY_PATH = (Get-ItemProperty -Path "HKCU:\Environment" -Name "KALSHI_PRIVATE_KEY_PATH" -ErrorAction Stop).KALSHI_PRIVATE_KEY_PATH
    } catch {}
}
$env:PYTHONIOENCODING = "utf-8"

"=== $(Get-Date -Format 'u') kalshi-daily-recap start ===" | Add-Content -Path $LogPath -Encoding utf8
"KALSHI_PRIVATE_KEY_PATH=$env:KALSHI_PRIVATE_KEY_PATH" | Add-Content -Path $LogPath -Encoding utf8

# Non-interactive claude session. bypassPermissions since there is no user to
# approve tool calls; the workflow only touches this repo and localhost.
$Prompt = "Run the kalshi-daily-recap scheduled task exactly as defined in C:\Users\jackd\.claude\scheduled-tasks\kalshi-daily-recap\SKILL.md. Follow every step. When done, print the narrative and the four dashboard-location lines as your final response."

& $Claude -p $Prompt `
    --permission-mode bypassPermissions `
    --output-format text `
    2>&1 | Tee-Object -FilePath $LogPath -Append

$ExitCode = $LASTEXITCODE
"=== $(Get-Date -Format 'u') kalshi-daily-recap exit=$ExitCode ===" | Add-Content -Path $LogPath -Encoding utf8

# Never fail silently (the 7/6-7/8 "Not logged in" streak went unnoticed for
# 3 days): on nonzero exit, email the log tail via the ALERT_EMAIL_* env vars.
if ($ExitCode -ne 0) {
    foreach ($n in 'ALERT_EMAIL_FROM','ALERT_EMAIL_PASSWORD','ALERT_EMAIL_TO') {
        if (-not (Get-Item "env:$n" -ErrorAction SilentlyContinue)) {
            try { Set-Item "env:$n" (Get-ItemProperty -Path "HKCU:\Environment" -Name $n -ErrorAction Stop).$n } catch {}
        }
    }
    $tail = (Get-Content $LogPath -Tail 8) -join "`n"
    python (Join-Path $Repo "send_alert_email.py") `
        "kalshi-daily-recap FAILED (exit=$ExitCode)" `
        "The 8:33 AM dashboard run failed. Log: $LogPath`n`n$tail" `
        2>&1 | Add-Content -Path $LogPath -Encoding utf8
}

exit $ExitCode
