# Launcher for the kalshi-daily-recap Claude Code skill under Windows Task
# Scheduler. Fires claude.exe -p in non-interactive mode so it can wake the
# PC at 8:30 AM, run the recap workflow, and let the machine go back to sleep.
#
# Logs to run-logs\kalshi-daily-recap\YYYY-MM-DD.log.
#
#   run_kalshi_daily_recap.ps1 -CheckLogin   # test the login with one tiny run, nothing else

param([switch]$CheckLogin)

$Claude ="C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
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

# ONE-YEAR LOGIN (Jack 2026-10-12: "any other things need this login so it
# doesnt fail suddenly?"). The 10/12 5:45 AM run died on "Failed to
# authenticate: OAuth session expired and could not be refreshed", the same
# expired interactive login that stopped the IMM family watch's research. A
# `claude setup-token` token saved in ~/.claude/imm-research-token.txt (one
# file for both jobs; imm_family_watch.py reads it the same way) is set for
# THIS process only, so claude.exe below runs on it, not on the /login
# credentials. The API-key variables outrank it and would bill the API per
# token, so they are dropped. No file / no token = the stored login, as
# before. The token is never logged.
$Auth = "stored login"
$TokenFile = Join-Path $env:USERPROFILE ".claude\imm-research-token.txt"
if (Test-Path $TokenFile) {
    $raw = [string](Get-Content -Raw -Path $TokenFile -ErrorAction SilentlyContinue)
    $m = [regex]::Match($raw, 'sk-ant-[A-Za-z0-9_-]{20,}')
    $tok = if ($m.Success) { $m.Value } elseif ($raw.Trim() -and $raw.Trim() -notmatch '\s') { $raw.Trim() } else { "" }
    if ($tok) {
        $env:CLAUDE_CODE_OAUTH_TOKEN = $tok
        Remove-Item Env:ANTHROPIC_API_KEY, Env:ANTHROPIC_AUTH_TOKEN -ErrorAction SilentlyContinue
        $Auth = "token file"
    } else {
        $Auth = "stored login (no token in the token file)"
    }
}

if ($CheckLogin) {
    $out = (& $Claude -p "Reply with the single word OK." --output-format text 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $out -match 'OK') {
        "recap login OK via $Auth"
        exit 0
    }
    "recap login FAILED via ${Auth}: $out"
    exit 1
}

"=== $(Get-Date -Format 'u') kalshi-daily-recap start ===" | Add-Content -Path $LogPath -Encoding utf8
"KALSHI_PRIVATE_KEY_PATH=$env:KALSHI_PRIVATE_KEY_PATH" | Add-Content -Path $LogPath -Encoding utf8
"auth: $Auth" | Add-Content -Path $LogPath -Encoding utf8

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
    $fix = ""
    if ($tail -match 'failed to authenticate|oauth (session|token)|not logged in|run /login|invalid api key') {
        $fix = if ($Auth -eq "token file") {
            "`n`nLOGIN: the token in $TokenFile was rejected (expired or revoked). " +
            "Run 'claude setup-token' in a terminal and paste the new token over the file's contents."
        } else {
            "`n`nLOGIN: run 'claude setup-token' in a terminal and paste the token into " +
            "$TokenFile (one-year login), or sign in again with 'claude' then /login."
        }
    }
    python (Join-Path $Repo "send_alert_email.py") `
        "kalshi-daily-recap FAILED (exit=$ExitCode)" `
        "The 8:33 AM dashboard run failed ($Auth). Log: $LogPath`n`n$tail$fix" `
        2>&1 | Add-Content -Path $LogPath -Encoding utf8
}

exit $ExitCode
