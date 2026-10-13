# Launcher for the one-time review of the IMM CPI core pilot (KXCPICORE and
# KXCPICOREYOY, live since 2026-09-29) under Windows Task Scheduler: "KL
# cpi-pilot-report", once at 8:00 AM ET Tue 2026-10-13, through
# run_cpi_pilot_report_hidden.vbs so no console window shows.
#
# It replaces the Claude Desktop scheduled task "cpi-core-pilot-report"
# (disabled 2026-10-12; Jack chose this over a hand run). Desktop runs use the
# "default" permission mode, so that run would have stalled on its first
# command, `python imm_cpi_pilot_report.py` (not in KL's allow-list), as every
# high-temp-daily-digest run did from 7/27. Same pattern as
# run_high_temp_digest.ps1: claude.exe -p with bypassPermissions, the
# one-year token file, its own worktree, an email either way. The prompt is
# still that task's SKILL.md (keep the disabled task: its folder holds the
# file), with the overrides in $PromptTemplate below.
#
# A run:
#  1. claude -p, in its own worktree $Wt (detached at origin/main; never in
#     KL, the live bot's checkout), runs KL's imm_cpi_pilot_report.py. The
#     script is read-only (Kalshi reads, KL's ledgers) and writes only
#     run-logs\incentive-mm\cpi_pilot_report_<ET date>.md. Claude's answer is
#     the short keep/block review. A run with no answer or no report file is
#     retried once, 10 min later (a PC just woken may have no network yet).
#  2. This script checks the report file was written, renders the review and
#     the script's full report as one page (md_report_email.py) and emails it
#     (send_html_email.py). A failure emails the log tail, and the script's
#     report when it got that far.
#  3. Removes the worktree.
#
#   run_cpi_pilot_report.ps1               # the scheduled run
#   run_cpi_pilot_report.ps1 -NoEmail      # a test: the same run, no email (the page stays in the log dir)
#   run_cpi_pilot_report.ps1 -CheckLogin   # test the login only
#
# Logs to run-logs\cpi-pilot-report\YYYY-MM-DD.log (the day of the run).

param(
    [switch]$NoEmail,
    [switch]$CheckLogin
)

$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$Wt = "C:\Users\jackd\Documents\KL-wt-cpi-pilot-report"
$Skill = "C:\Users\jackd\.claude\scheduled-tasks\cpi-core-pilot-report\SKILL.md"
$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
$ReportDir = Join-Path $Repo "run-logs\incentive-mm"
$ClaudeTimeoutMin = 40
$RetryWaitMin = 10

$LogDir = Join-Path $Repo "run-logs\cpi-pilot-report"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("{0:yyyy-MM-dd}.log" -f (Get-Date))
function Log([string]$Line) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Line" | Add-Content -Path $LogPath -Encoding utf8
}

# Task Scheduler sessions do not reliably inherit user env vars (see
# run_kalshi_daily_recap.ps1): take them from HKCU. KALSHI_PRIVATE_KEY_PATH
# is the key the report signs with while the IMM has no key of its own
# (imm_account.py); requests and cryptography live in the user site, which
# the "KL imm quote-gaps" task also puts on PYTHONPATH.
foreach ($n in 'KALSHI_PRIVATE_KEY_PATH','ALERT_EMAIL_FROM','ALERT_EMAIL_PASSWORD','ALERT_EMAIL_TO') {
    if (-not (Get-Item "env:$n" -ErrorAction SilentlyContinue)) {
        try { Set-Item "env:$n" (Get-ItemProperty -Path "HKCU:\Environment" -Name $n -ErrorAction Stop).$n } catch {}
    }
}
if ((Test-Path $UserSite) -and (($env:PYTHONPATH -split ';') -notcontains $UserSite)) {
    $env:PYTHONPATH = (@($UserSite) + @($env:PYTHONPATH -split ';' | Where-Object { $_ })) -join ';'
}
$env:PYTHONIOENCODING = "utf-8"

# ONE-YEAR LOGIN, read exactly as run_kalshi_daily_recap.ps1 and
# imm_family_watch.py read it: the `claude setup-token` token saved in
# ~/.claude/imm-research-token.txt is set for THIS process only. The API-key
# variables outrank it and would bill the API, so they are dropped. No file
# or no token = the stored login. The token is never logged.
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
    Remove-Variable raw, m, tok -ErrorAction SilentlyContinue
}

if ($CheckLogin) {
    $out = (& $Claude -p "Reply with the single word OK." --output-format text 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -eq 0 -and $out -match 'OK') {
        "cpi report login OK via $Auth"
        exit 0
    }
    "cpi report login FAILED via ${Auth}: $out"
    exit 1
}

$LoginError = 'failed to authenticate|oauth (session|token)|not logged in|run /login|invalid api key'

function Invoke-Git {
    $out = (& git @args 2>&1 | ForEach-Object { "$_" }) -join "`n"
    [pscustomobject]@{ Ok = ($LASTEXITCODE -eq 0); Out = $out }
}

function Get-EtNow {
    [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Eastern Standard Time')
}

function Reset-Worktree {
    $f = Invoke-Git -C $Repo fetch origin
    if (-not $f.Ok) {
        Start-Sleep -Seconds 20   # the sync's own fetch may hold the ref lock
        $f = Invoke-Git -C $Repo fetch origin
    }
    # the worktree is only code to read; the last fetched origin/main will do
    if (-not $f.Ok) { Log "git fetch failed, using the last fetched origin/main: $($f.Out)" }
    if (-not (Test-Path (Join-Path $Wt ".git"))) {
        if (Test-Path $Wt) {   # a worktree remove that died half way
            Remove-Item -Recurse -Force $Wt -ErrorAction SilentlyContinue
            if (Test-Path $Wt) { throw "$Wt exists and is not a worktree; could not clear it" }
        }
        Invoke-Git -C $Repo worktree prune | Out-Null   # a removed worktree's leftover entry
        $a = Invoke-Git -C $Repo worktree add --detach $Wt origin/main
        if (-not $a.Ok) { throw "git worktree add $Wt failed: $($a.Out)" }
        Log "created worktree $Wt"
        return
    }
    $c = Invoke-Git -C $Wt checkout --detach --force origin/main
    if (-not $c.Ok) { throw "worktree checkout failed: $($c.Out)" }
    Invoke-Git -C $Wt clean -fdq | Out-Null   # the last run's scratch files
}

function Remove-Worktree {
    if (-not (Test-Path $Wt)) { return }
    $r = Invoke-Git -C $Repo worktree remove --force $Wt
    Log $(if ($r.Ok) { "removed worktree $Wt" } else { "worktree remove failed: $($r.Out)" })
}

# The prompt goes in on stdin: no command-line quoting, and keep it ASCII
# (Windows PowerShell writes stdin in the OEM code page).
function Invoke-Claude([string]$Prompt) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Claude
    $psi.Arguments = '-p --permission-mode bypassPermissions --output-format text'
    $psi.WorkingDirectory = $Wt
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $p = [System.Diagnostics.Process]::Start($psi)
    $p.StandardInput.Write($Prompt)
    $p.StandardInput.Close()
    $so = $p.StandardOutput.ReadToEndAsync()
    $se = $p.StandardError.ReadToEndAsync()
    $exited = $p.WaitForExit($ClaudeTimeoutMin * 60 * 1000)
    if (-not $exited) {
        & taskkill.exe /T /F /PID $p.Id 2>&1 | Out-Null
        $p.WaitForExit(30000) | Out-Null
    }
    $so.Wait(120000) | Out-Null
    $se.Wait(10000) | Out-Null
    [pscustomobject]@{
        TimedOut = (-not $exited)
        ExitCode = $(if ($exited) { $p.ExitCode } else { -1 })
        Out = $(if ($so.IsCompleted) { $so.Result } else { "" })
        Err = $(if ($se.IsCompleted) { $se.Result } else { "" })
    }
}

function Send-Alert([string]$Subject, [string]$Body) {
    if ($NoEmail) { return }
    $o = & $Python (Join-Path $PSScriptRoot "send_alert_email.py") $Subject $Body.Replace('"', "'") 2>&1 | ForEach-Object { "$_" }
    Log "alert email: $($o -join ' | ')"
}

# A one-time report: one send failing should not lose it.
function Send-Page([string]$Subject, [string]$Page, [string]$Text) {
    for ($i = 1; $i -le 3; $i++) {
        $o = & $Python (Join-Path $PSScriptRoot "send_html_email.py") $Subject.Replace('"', "'") $Page $Text.Replace('"', "'") 2>&1 | ForEach-Object { "$_" }
        $ok = ($LASTEXITCODE -eq 0)
        Log "report email (try $i): $($o -join ' | ')"
        if ($ok) { return $true }
        if ($i -lt 3) { Start-Sleep -Seconds 120 }
    }
    $false
}

$PromptTemplate = @'
Write Jack's one-time review of the IMM CPI core pilot. The task is defined in {SKILL}: read that file first and follow it, except where the overrides below differ. The overrides win.

This is an unattended run from Windows Task Scheduler (run_cpi_pilot_report.ps1 in the KL repo), replacing the Claude Desktop scheduled task of the same name. Nobody can answer questions, so make reasonable choices and say what you assumed.

OVERRIDES

1. Read-only, as the SKILL.md says, and more strictly: C:\Users\jackd\Documents\KL is the live trading bot's checkout. Do not cd into it, and do not create, change, move or delete anything in it; the report script in override 2 is the only thing you run from it. Your working directory, {WT} ({WTBASH} in bash), is a scratch git worktree of the KL repo at origin/main: read code there if you need to. Run no git command that changes anything, open no browser, and send no email (the launcher emails your answer).

2. SKILL.md step 1: run the report with exactly this command, from your working directory:
   {PYBASH} /c/Users/jackd/Documents/KL/imm_cpi_pilot_report.py
   No arguments: its defaults cover the pilot. It reads KL's ledgers and the Kalshi API, and it saves run-logs\incentive-mm\cpi_pilot_report_<date>.md in KL. That file is expected, and it is the only thing this run writes in KL. If the command fails, retry it once, then report the error. Do not modify any code.

3. Scope: since 2026-10-01 the pilot also quotes the whole KXCPICOREYOY family (core CPI, year over year), and the script includes it by default. The SKILL.md predates that. Review everything the script reports.

4. Today is {TODAY} (ET). Judge the freshness of reward_credits.csv (SKILL.md step 2) against that.

5. Your answer: the final message is the review Jack reads. It is emailed as is, as Markdown, and the launcher puts the script's full report under it, so do not paste or repeat that report or the script's output. Follow the SKILL.md's shape: the bottom line first, about 15 lines, one Markdown table, and the saved report's path at the end. No preamble about the steps you took. Then, as the very last line, exactly:
REPORT_SUMMARY: <recommend keep | recommend block | borderline> - net <signed dollars> over <days> days, 24h mark-out <signed cents>c
For example: REPORT_SUMMARY: recommend keep - net +$12.34 over 14.2 days, 24h mark-out -1.5c
'@

# One attempt: a fresh worktree, then claude -p. Throws when there is no answer.
function Invoke-Review([string]$Prompt) {
    Reset-Worktree
    Log "claude -p starting in $Wt"
    $t0 = Get-Date
    $r = Invoke-Claude $Prompt
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    Log "claude exit=$($r.ExitCode) after $mins min$(if ($r.TimedOut) { ' (TIMED OUT, killed)' })"
    $r.Out.TrimEnd() | Add-Content -Path $LogPath -Encoding utf8
    if ($r.Err.Trim()) { "stderr: $($r.Err.TrimEnd())" | Add-Content -Path $LogPath -Encoding utf8 }
    if ($r.TimedOut) { throw "claude -p ran past $ClaudeTimeoutMin min and was killed" }
    if ($r.ExitCode -ne 0) { throw "claude -p exited $($r.ExitCode)" }
    if (-not $r.Out.Trim()) { throw "claude -p printed no answer" }
    $r
}

function Find-Report([datetime]$Since) {
    Get-ChildItem $ReportDir -Filter 'cpi_pilot_report_*.md' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -ge $Since } | Sort-Object LastWriteTime | Select-Object -Last 1
}

# ---------------------------------------------------------------- main

Log "=== CPI pilot report start (auth: $Auth$(if ($NoEmail) { '; -NoEmail, a test' })) ==="
if (-not (Test-Path $Skill)) {
    Log "! the prompt file $Skill is missing"
    Send-Alert "CPI pilot report FAILED (no prompt file)" "run_cpi_pilot_report.ps1: $Skill is missing (was the desktop task deleted?). Nothing ran. The report script itself: python imm_cpi_pilot_report.py in KL. Log: $LogPath"
    exit 1
}
$wtBash = '/c' + $Wt.Substring(2).Replace('\', '/')
$pyBash = '/c' + $Python.Substring(2).Replace('\', '/')
$prompt = $PromptTemplate.Replace('{SKILL}', $Skill).Replace('{WTBASH}', $wtBash).Replace('{WT}', $Wt)
$prompt = $prompt.Replace('{PYBASH}', $pyBash).Replace('{TODAY}', (Get-EtNow).ToString('yyyy-MM-dd, dddd', [Globalization.CultureInfo]::InvariantCulture))

$started = Get-Date
$exit = 1
$res = $null
$report = $null
$err = ""
try {
    # Two attempts, 10 min apart. An attempt counts when claude answered AND
    # the script wrote its report: at a wake-up with no network yet, claude
    # answers fine, with "the script failed".
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $t0 = Get-Date
        $res = $null
        try {
            $res = Invoke-Review $prompt
        } catch {
            $err = $_.Exception.Message
            Log "! attempt ${attempt}: $err"
            if (((Get-Content $LogPath -Tail 15) -join "`n") -match $LoginError) { break }   # a retry will not fix the login
        }
        $report = Find-Report $t0
        if ($res -and $report) { break }
        if ($res) { Log "! attempt ${attempt}: claude answered, but no cpi_pilot_report_*.md was written in $ReportDir" }
        if ($attempt -lt 2) {
            Log "retrying in $RetryWaitMin min"
            Start-Sleep -Seconds ($RetryWaitMin * 60)
        }
    }

    if (-not $res) {
        $report = Find-Report $started
        $tail = (Get-Content $LogPath -Tail 15) -join "`n"
        $fix = ""
        if ($tail -match $LoginError) {
            $fix = "`n`nLOGIN ($Auth): run 'claude setup-token' in a terminal and paste the token over the contents of $TokenFile."
        }
        $extra = ""
        if ($report) {
            $extra = "`n`nThe report script did run. Its report ($($report.FullName)):`n`n" + [System.IO.File]::ReadAllText($report.FullName)
        }
        Send-Alert "CPI pilot report FAILED" "run_cpi_pilot_report.ps1 got no review from claude -p ($err). Log: $LogPath`n`n$tail$fix$extra"
    } else {
        if ($report) { Log "report file: $($report.FullName)" }
        $m = [regex]::Matches($res.Out, '(?m)^\s*REPORT_SUMMARY:\s*(.+?)\s*$')
        $summary = if ($m.Count) { $m[$m.Count - 1].Groups[1].Value } else { "" }
        $review = ([regex]::Replace($res.Out, '(?m)^\s*REPORT_SUMMARY:.*$', '')).Trim()
        $subject = if (-not $report) {
            "CPI pilot report: the report script FAILED (see the review)"
        } elseif ($summary) {
            "CPI pilot report: $summary"
        } else {
            "CPI pilot report"
        }

        # one page: the review on top, the script's full report under it
        $stamp = Get-Date -Format 'yyyy-MM-dd_HHmm'
        $reviewMd = Join-Path $LogDir "review_$stamp.md"
        [System.IO.File]::WriteAllText($reviewMd, "$review`n", (New-Object System.Text.UTF8Encoding($false)))
        $page = Join-Path $LogDir "cpi_pilot_report_$stamp.html"
        $mdFiles = @($reviewMd)
        if ($report) { $mdFiles += $report.FullName }
        $o = & $Python (Join-Path $PSScriptRoot "md_report_email.py") $page "CPI core pilot report" @mdFiles 2>&1 | ForEach-Object { "$_" }
        Log "page: $($o -join ' | ')"
        Log "subject: $subject"

        $sent = $false
        if ($NoEmail) {
            Log "-NoEmail: nothing sent; the page is $page"
            $sent = $true
        } elseif (Test-Path $page) {
            $sent = Send-Page $subject $page "$review`n`nLog: $LogPath"
        } else {
            Send-Alert $subject "$review`n`n(The HTML page did not render, so this is the plain text. Log: $LogPath)"
        }
        if ($report -and $sent) { $exit = 0 }
    }
} catch {
    Log "! launcher error: $($_.Exception.Message)"
    Send-Alert "CPI pilot report FAILED (launcher error)" "run_cpi_pilot_report.ps1 stopped: $($_.Exception.Message). Log: $LogPath"
} finally {
    Remove-Worktree
}
Log "=== CPI pilot report done: $(if ($exit -eq 0) { 'ok' } else { 'FAILED or incomplete, see above' }) ==="
exit $exit
