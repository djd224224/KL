# Launcher for the one-time YouTube markets re-score (the Oct 15 verdict on
# the IMM's YouTube families and its two YouTube pilots) under Windows Task
# Scheduler: "KL yt-rescore", once at 11:00 AM ET Thu 2026-10-15, through
# run_yt_rescore_hidden.vbs so no console window shows.
#
# It replaces the paused Claude Desktop scheduled task
# "youtube-markets-rescore-oct15" (Jack 2026-10-12: "do the same for the
# youtube rescore on 10/15", after run_cpi_pilot_report.ps1). Desktop runs use
# the "default" permission mode and stall on their first shell command. Same
# pattern as run_cpi_pilot_report.ps1: claude.exe -p with bypassPermissions,
# the one-year token file, an email either way. The prompt is still that
# task's SKILL.md (keep the disabled task: its folder holds the file), with the
# overrides in $PromptTemplate below.
#
# ONE RUN ONLY. The "IMM bot improvements" desktop session holds a
# session-only cron (beccf004) that runs the same SKILL.md at 10:03 ET 10/15
# and emails its own verdict; it fires only if that session is still open
# then. Both runs would write KL-data\youtube-analysis-2026-10-15 and pace the
# same rate-limited endpoints, so this job starts later and STANDS DOWN when
# that folder already exists (another run started it), emailing a short note.
# -Force runs anyway.
#
# A run:
#  1. The stand-down check; then it creates the output folder with a
#     RUN_BY.txt saying who owns it.
#  2. claude -p works IN that folder (the SKILL.md's only write location) and
#     writes verdict.md. It reads KL, the live bot's checkout, by absolute
#     path only, and sends no email: this script does.
#  3. This script checks verdict.md, renders it (md_report_email.py) and
#     emails it (send_html_email.py; the same inbox as the IMM alerter the
#     SKILL.md names). An attempt that ends inside 20 min with no verdict is
#     retried once, 10 min later (a PC just woken may have no network yet); a
#     failure emails the log tail and claude's answer.
#
#   run_yt_rescore.ps1                     # the scheduled run
#   run_yt_rescore.ps1 -Force              # run even though the output folder exists
#   run_yt_rescore.ps1 -DryRun -NoEmail    # plumbing test: a stub verdict in KL-data\youtube-analysis-dryrun
#   run_yt_rescore.ps1 -CheckLogin         # test the login only
#
# Logs to run-logs\yt-rescore\YYYY-MM-DD.log (the day of the run).

param(
    [switch]$NoEmail,
    [switch]$CheckLogin,
    [switch]$Force,
    [switch]$DryRun
)

$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$Skill = "C:\Users\jackd\.claude\scheduled-tasks\youtube-markets-rescore-oct15\SKILL.md"
$Memory = "C:\Users\jackd\.claude\projects\C--Users-jackd-Documents\memory\imm-yt-weekly-pilot.md"
$OutDir = if ($DryRun) { "C:\Users\jackd\Documents\KL-data\youtube-analysis-dryrun" } else { "C:\Users\jackd\Documents\KL-data\youtube-analysis-2026-10-15" }
$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
$Subject = "IMM YouTube markets re-score (Oct 15) - verdict"
$ClaudeTimeoutMin = 180
$QuickFailMin = 20
$RetryWaitMin = 10

$LogDir = Join-Path $Repo "run-logs\yt-rescore"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("{0:yyyy-MM-dd}.log" -f (Get-Date))
function Log([string]$Line) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Line" | Add-Content -Path $LogPath -Encoding utf8
}

# Task Scheduler sessions do not reliably inherit user env vars (see
# run_kalshi_daily_recap.ps1): take them from HKCU. KALSHI_PRIVATE_KEY_PATH
# is the key kalshi_reads signs with while the IMM has no key of its own
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
        "yt re-score login OK via $Auth"
        exit 0
    }
    "yt re-score login FAILED via ${Auth}: $out"
    exit 1
}

$LoginError = 'failed to authenticate|oauth (session|token)|not logged in|run /login|invalid api key'

# The prompt goes in on stdin: no command-line quoting, and keep it ASCII
# (Windows PowerShell writes stdin in the OEM code page).
function Invoke-Claude([string]$Prompt) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Claude
    $psi.Arguments = '-p --permission-mode bypassPermissions --output-format text'
    $psi.WorkingDirectory = $OutDir
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

# A one-time verdict: one send failing should not lose it.
function Send-Page([string]$Subject, [string]$Page, [string]$Text) {
    for ($i = 1; $i -le 3; $i++) {
        $o = & $Python (Join-Path $PSScriptRoot "send_html_email.py") $Subject.Replace('"', "'") $Page $Text.Replace('"', "'") 2>&1 | ForEach-Object { "$_" }
        $ok = ($LASTEXITCODE -eq 0)
        Log "verdict email (try $i): $($o -join ' | ')"
        if ($ok) { return $true }
        if ($i -lt 3) { Start-Sleep -Seconds 120 }
    }
    $false
}

$PromptTemplate = @'
Run the one-time YouTube markets re-score for Jack. The task is defined in {SKILL}: read that file first and follow it, except where the overrides below differ. The overrides win.

This is an unattended run from Windows Task Scheduler (run_yt_rescore.ps1 in the KL repo), replacing the paused Claude Desktop scheduled task of the same name. Its header line describes the schedule, not this run: the launcher has already checked that no other re-score run started, so do the re-score. Nobody can answer questions, so make reasonable choices and say what you assumed.

OVERRIDES

1. Where to work: your working directory, {OUT} ({OUTBASH} in bash), is the SKILL.md's output folder, already created. Write scripts and outputs only there. C:\Users\jackd\Documents\KL is the live trading bot's checkout: do not cd into it and do not create, change, move or delete anything in it. Read its files by absolute path, import from it with sys.path as the SKILL.md shows, and run only read-only git commands against it, with git -C. Run Python as {PYBASH}, the interpreter with the repo's packages. RUN_BY.txt in your folder is the launcher's; leave it.

2. Do NOT send the email yourself: skip the SKILL.md's alerter step. Save verdict.md in your working directory as the SKILL.md describes (ASCII-safe, under ~500 words plus compact tables). The launcher emails it once, to the same inbox as the KL alerter.

3. Scope, from the 10/10 plan this run replaces: score BOTH live YouTube pilots, keeping the pre-registered hypotheses as stated. They are the #2 top-video pilot (KXYTTOPVIDEO2D and KXYTTOPVIDEOG2D, live since 2026-10-05 16:01Z) and the KXYTVIEWSW weekly pilot (KAT, DRA, WEE and TAT only, live since 2026-10-06 12:38Z). Give KEEP / CHANGE / STOP for each, with the numbers. For their live P&L use the bot's own logs in C:\Users\jackd\Documents\KL\run-logs\incentive-mm\: trading = the sum of realized_delta_dollars in realized_*.jsonl (it already includes settlements; the per-run realized_total resets on restart) plus open positions at their latest marks_*.jsonl mark; reward = cycle_log est_frac x pool_per_day per cycle (gaps capped at 300 s), which tracked Kalshi's credits within ~2% (reward_report_*.html). Compare with the 10/10 baseline in the "2026-10-10 status" section of {MEMORY} (if that section is gone, say so and compare with the SKILL.md's 10/05 numbers).

4. Time: the launcher stops this run after {TIMEOUT} minutes, so write verdict.md well before then; a verdict that marks its gaps beats none. The Bash tool caps one command at 10 minutes, so run the slow charts.youtube.com pull in the background (the Bash tool's run_in_background), keep the SKILL.md's pacing and its stop-after-3-429s rule, do the other steps meanwhile, and do not end your run while the pull is still going.

5. Once verdict.md is saved, append one short dated outcome line to {MEMORY}, for example: "2026-10-15 re-score (Windows task): yt2 <call> net <x>/day; ytw <call>; families <calls>". Change nothing else in that file.

6. Your final message: three to six lines for the launcher's log, with the per-family verdicts and the two pilot calls. Then, as the very last line, exactly:
RESCORE_SUMMARY: <one line under 120 characters: the two pilot calls, then the family verdicts>
'@

$DryRunPrompt = @'
DRY RUN of run_yt_rescore.ps1 (Windows Task Scheduler): a plumbing test. Do NOT do the re-score analysis, and send no email.

Your working directory, {OUT} ({OUTBASH} in bash), is a scratch folder. C:\Users\jackd\Documents\KL is the live trading bot's checkout: do not cd into it and write nothing there.

1. Read the first 20 lines of {SKILL}.
2. List the newest file times in C:\Users\jackd\Documents\KL-data\youtube-collect\ (are the collectors' logs fresh?).
3. Make ONE Kalshi read through the repo helper, running a small script with {PYBASH}:
   import sys; sys.path.insert(0, "C:/Users/jackd/Documents/KL")
   from kalshi_reads import kalshi_get
   print(kalshi_get("/exchange/status"))
   Report only whether it succeeded and the status fields. Never print key material.
4. Write verdict.md in your working directory: a heading "DRY RUN", then a short Markdown table of the collectors' newest file times, the Kalshi read result and the time.
5. Final message: one line, then as the very last line exactly:
RESCORE_SUMMARY: dry run ok
'@

# One attempt. Throws when claude fails; a verdict.md is checked by the caller.
function Invoke-Rescore([string]$Prompt) {
    Log "claude -p starting in $OutDir"
    $t0 = Get-Date
    $r = Invoke-Claude $Prompt
    $mins = [math]::Round(((Get-Date) - $t0).TotalMinutes, 1)
    Log "claude exit=$($r.ExitCode) after $mins min$(if ($r.TimedOut) { ' (TIMED OUT, killed)' })"
    $r.Out.TrimEnd() | Add-Content -Path $LogPath -Encoding utf8
    if ($r.Err.Trim()) { "stderr: $($r.Err.TrimEnd())" | Add-Content -Path $LogPath -Encoding utf8 }
    if ($r.TimedOut) { throw "claude -p ran past $ClaudeTimeoutMin min and was killed" }
    if ($r.ExitCode -ne 0) { throw "claude -p exited $($r.ExitCode)" }
    $r
}

# ---------------------------------------------------------------- main

$mail = if ($env:ALERT_EMAIL_FROM -and $env:ALERT_EMAIL_PASSWORD) { "set" } else { "NOT SET, no email can go out" }
$flags = @($(if ($DryRun) { 'DRY RUN' }), $(if ($NoEmail) { '-NoEmail' }), $(if ($Force) { '-Force' })) | Where-Object { $_ }
Log "=== YouTube re-score start (auth: $Auth; email account: $mail$(if ($flags) { '; ' + ($flags -join ', ') })) ==="
if (-not (Test-Path $Skill)) {
    Log "! the prompt file $Skill is missing"
    Send-Alert "IMM YouTube re-score FAILED (no prompt file)" "run_yt_rescore.ps1: $Skill is missing (was the desktop task deleted?). Nothing ran. Log: $LogPath"
    exit 1
}

# ONE RUN ONLY (see the header): another run created the folder first.
if ((Test-Path $OutDir) -and -not $Force) {
    $files = @(Get-ChildItem $OutDir -Recurse -File -ErrorAction SilentlyContinue)
    $newest = $files | Sort-Object LastWriteTime | Select-Object -Last 1
    $verdictFile = Join-Path $OutDir "verdict.md"
    $state = if (Test-Path $verdictFile) {
        "verdict.md is already there (written $((Get-Item $verdictFile).LastWriteTime.ToString('yyyy-MM-dd HH:mm')))"
    } elseif ($newest) {
        "no verdict.md yet; the newest of its $($files.Count) files was written $($newest.LastWriteTime.ToString('yyyy-MM-dd HH:mm')), so another run is probably still working"
    } else {
        "it is empty"
    }
    Log "stood down: $OutDir exists ($state)"
    Send-Alert "IMM YouTube re-score: the Windows task stood down (another run started it)" "run_yt_rescore.ps1 found $OutDir already there at $(Get-Date -Format 'yyyy-MM-dd HH:mm') ET: $state. Another run started the re-score, most likely the 'IMM bot improvements' session's cron at 10:03 ET, which emails its own verdict, so this task did not start a second one. If no verdict email arrives, run: powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Force`n`nLog: $LogPath"
    Log "=== YouTube re-score done: stood down ==="
    exit 0
}
New-Item -ItemType Directory -Force $OutDir | Out-Null
"Owned by the Windows task 'KL yt-rescore' (run_yt_rescore.ps1 in the KL repo), started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ET. One re-score run per folder: another run that finds this file should leave this folder alone." |
    Set-Content -Path (Join-Path $OutDir "RUN_BY.txt") -Encoding ascii

$outBash = '/c' + $OutDir.Substring(2).Replace('\', '/')
$pyBash = '/c' + $Python.Substring(2).Replace('\', '/')
$template = if ($DryRun) { $DryRunPrompt } else { $PromptTemplate }
$prompt = $template.Replace('{SKILL}', $Skill).Replace('{OUTBASH}', $outBash).Replace('{OUT}', $OutDir)
$prompt = $prompt.Replace('{MEMORY}', $Memory).Replace('{TIMEOUT}', [string]$ClaudeTimeoutMin).Replace('{PYBASH}', $pyBash)

$verdictPath = Join-Path $OutDir "verdict.md"
$exit = 1
$res = $null
$verdict = $null
$err = ""
try {
    # Two attempts only when the first ends quickly with no verdict; a long
    # attempt is not repeated (it already spent the rate-limited pulls).
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $t0 = Get-Date
        $res = $null
        try {
            $res = Invoke-Rescore $prompt
        } catch {
            $err = $_.Exception.Message
            Log "! attempt ${attempt}: $err"
            if (((Get-Content $LogPath -Tail 15) -join "`n") -match $LoginError) { break }   # a retry will not fix the login
        }
        $verdict = Get-Item $verdictPath -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -ge $t0 }
        if ($verdict) { break }
        if ($res) { Log "! attempt ${attempt}: claude answered, but wrote no verdict.md" }
        $mins = ((Get-Date) - $t0).TotalMinutes
        if ($attempt -ge 2 -or $mins -ge $QuickFailMin) { break }
        Log "retrying in $RetryWaitMin min (the attempt ended after $([math]::Round($mins, 1)) min)"
        Start-Sleep -Seconds ($RetryWaitMin * 60)
    }

    if (-not $verdict) {
        $tail = (Get-Content $LogPath -Tail 15) -join "`n"
        $fix = ""
        if ($tail -match $LoginError) {
            $fix = "`n`nLOGIN ($Auth): run 'claude setup-token' in a terminal and paste the token over the contents of $TokenFile."
        }
        $answer = if ($res) { "`n`nclaude's answer:`n" + $res.Out.Trim() } else { "" }
        Send-Alert "IMM YouTube re-score FAILED" "run_yt_rescore.ps1 got no verdict.md in $OutDir ($(if ($err) { $err } else { 'claude finished without writing it' })). Partial outputs stay in that folder. To run it again: powershell -NoProfile -ExecutionPolicy Bypass -File $PSCommandPath -Force`n`nLog: $LogPath`n`n$tail$fix$answer"
    } else {
        Log "verdict: $($verdict.FullName) ($($verdict.Length) bytes)"
        $summary = ""
        if ($res) {
            $m = [regex]::Matches($res.Out, '(?m)^\s*RESCORE_SUMMARY:\s*(.+?)\s*$')
            if ($m.Count) { $summary = $m[$m.Count - 1].Groups[1].Value }
        }
        $subj = $Subject + $(if ($summary) { ": $summary" } else { "" }) + $(if (-not $res) { " [the run did not finish cleanly]" } else { "" })
        if ($DryRun) { $subj = "[DRY RUN] $subj" }
        $page = Join-Path $LogDir ("yt_rescore_{0}.html" -f (Get-Date -Format 'yyyy-MM-dd_HHmm'))
        $o = & $Python (Join-Path $PSScriptRoot "md_report_email.py") $page "IMM YouTube markets re-score" $verdict.FullName 2>&1 | ForEach-Object { "$_" }
        Log "page: $($o -join ' | ')"
        Log "subject: $subj"
        $text = [System.IO.File]::ReadAllText($verdict.FullName) + "`n`nFolder: $OutDir`nLog: $LogPath"
        $sent = $false
        if ($NoEmail) {
            Log "-NoEmail: nothing sent; the page is $page"
            $sent = $true
        } elseif (Test-Path $page) {
            $sent = Send-Page $subj $page $text
        } else {
            Send-Alert $subj $text
        }
        if ($sent -and $res) { $exit = 0 }
    }
} catch {
    Log "! launcher error: $($_.Exception.Message)"
    Send-Alert "IMM YouTube re-score FAILED (launcher error)" "run_yt_rescore.ps1 stopped: $($_.Exception.Message). Log: $LogPath"
}
Log "=== YouTube re-score done: $(if ($exit -eq 0) { 'ok' } else { 'FAILED or incomplete, see above' }) ==="
exit $exit
