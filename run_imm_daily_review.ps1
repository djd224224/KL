# Launcher for the IMM daily review (Jack 2026-11-25: "create a skill that on a
# daily basis goes through IMM bot in incredible detail ... and suggests fixes
# to me in a morning email"). Windows Task Scheduler job "KL imm daily-review",
# daily 04:30 ET, through run_imm_daily_review_hidden.vbs (no console window).
# The email lands around 06:00-06:30, before the 07:00 cluster.
#
# A run, for ET day D = yesterday:
#  1. Snapshots the live tree's dirty/untracked files (the tripwire's "before").
#  2. python imm_review.py collect --day D: the evidence pack, no model, no
#     Kalshi calls, writes only run-logs\incentive-mm\daily_review\D\ (DIR).
#  3. claude -p (opus) runs the imm-daily-review skill in SCHEDULED mode in
#     the live tree, so it sees the code that is running and the project
#     memory. bypassPermissions + a deny list, the sms_inbox.py pattern (deny
#     rules hold in bypass mode): no Edit/Write/PowerShell, no git writes, no
#     deletes or moves, no texts, sends, restarts or list writers. It writes
#     only through `imm_review.py put` into DIR, then ingests its findings.
#     The full tool trace goes to DIR\claude_stream.jsonl.
#  4. Tripwire: any live-tree file outside run-logs that appeared or changed
#     during the run is emailed as an alert (it can also be another session).
#  5. Ingests findings.json if the run did not, then send_imm_review.py
#     emails review.md + the exact-number appendix. If the analysis produced
#     no review.md, the evidence-only fallback goes out instead. Never silent:
#     every failure emails the log tail (send_alert_email.py).
#
#   run_imm_daily_review.ps1                  # the scheduled run (yesterday, ET)
#   run_imm_daily_review.ps1 -Day 2026-11-24  # a given day
#   run_imm_daily_review.ps1 -NoEmail         # full run, email rendered but not sent
#   run_imm_daily_review.ps1 -SkipClaude      # collect + fallback only (tests the plumbing)
#   run_imm_daily_review.ps1 -Force           # run even if DIR\sent.marker exists
#   run_imm_daily_review.ps1 -CheckLogin      # test the login only
#   run_imm_daily_review.ps1 -CodeDir <worktree> -NoEmail
#                                             # a pre-merge test: code, skill and the claude run
#                                             # from the worktree; data and tripwire stay live
#
# Logs to DIR\run.log.

param(
    [string]$Day = "",
    [string]$CodeDir = "",
    [switch]$NoEmail,
    [switch]$SkipClaude,
    [switch]$Force,
    [switch]$CheckLogin
)

$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
$Repo = "C:\Users\jackd\Documents\KL"
if (-not $CodeDir) { $CodeDir = $Repo }
$Skill = Join-Path $CodeDir ".claude\skills\imm-daily-review\SKILL.md"
$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$UserSite = "C:\Users\jackd\AppData\Roaming\Python\Python312\site-packages"
$Model = "opus"
# 35 min (was 25): the 11/26 collect took 1,181 s of 1,500, the moat's cycle-log
# scan 1,050 s of that at BelowNormal priority beside the bot. The scan is now
# one shared pass (imm_review._cycle_pass); this is the margin on top.
# 35 + 100 + ingest 5 + send 15 = 155 min, inside the task's 2 h 45 min.
$CollectTimeoutMin = 35
$ClaudeTimeoutMin = 100
$LoginError = 'failed to authenticate|oauth (session|token)|not logged in|run /login|invalid api key'

# Everything the unattended run may not do (deny rules hold in bypass mode).
$Deny = @(
    'Edit', 'Write', 'NotebookEdit', 'PowerShell', 'WebFetch', 'WebSearch',
    'Bash(git push:*)', 'Bash(git commit:*)', 'Bash(git reset:*)', 'Bash(git checkout:*)',
    'Bash(git switch:*)', 'Bash(git stash:*)', 'Bash(git rebase:*)', 'Bash(git merge:*)',
    'Bash(git pull:*)', 'Bash(git fetch:*)', 'Bash(git cherry-pick:*)', 'Bash(git revert:*)',
    'Bash(git clean:*)', 'Bash(git restore:*)', 'Bash(git rm:*)', 'Bash(git mv:*)',
    'Bash(git add:*)', 'Bash(git worktree:*)', 'Bash(git branch:*)', 'Bash(git tag:*)',
    'Bash(git apply:*)', 'Bash(git am:*)', 'Bash(git config:*)',
    'Bash(rm:*)', 'Bash(del:*)', 'Bash(mv:*)', 'Bash(cp:*)', 'Bash(rmdir:*)', 'Bash(touch:*)',
    'Bash(tee:*)', 'Bash(sed -i:*)', 'Bash(curl:*)', 'Bash(wget:*)',
    'Bash(powershell:*)', 'Bash(powershell.exe:*)', 'Bash(pwsh:*)', 'Bash(schtasks:*)',
    'Bash(schtasks.exe:*)', 'Bash(taskkill:*)', 'Bash(cmd:*)', 'Bash(cmd.exe:*)',
    'Bash(wscript:*)', 'Bash(cscript:*)', 'Bash(start:*)',
    'Bash(python phone_actions.py:*)', 'Bash(python sms_alert.py:*)', 'Bash(python sms_inbox.py:*)',
    'Bash(python wave_alert.py:*)', 'Bash(python send_alert_email.py:*)',
    'Bash(python send_html_email.py:*)', 'Bash(python send_imm_review.py:*)',
    'Bash(python md_report_email.py:*)', 'Bash(python send_imm_digest.py:*)',
    'Bash(python send_portfolio_digest.py:*)', 'Bash(python send_opportunistic_imm.py:*)',
    'Bash(python imm_rows.py:*)', 'Bash(python imm_earnings_overrides.py:*)',
    'Bash(python pvd_waves.py:*)', 'Bash(python pvd_wave_tune.py:*)', 'Bash(python pvd_scan.py:*)',
    'Bash(python cheap_scan.py:*)', 'Bash(python relight_watch.py:*)', 'Bash(python bait_watch.py:*)',
    'Bash(python imm_family_watch.py:*)', 'Bash(python imm_dashboard.py:*)',
    'Bash(python imm_reward_recon.py:*)', 'Bash(python imm_quote_gaps.py:*)',
    'Bash(python imm_unquoted.py:*)', 'Bash(python incentive_mm.py:*)',
    'Bash(python kalshi_subaccount.py:*)', 'Bash(python cancel_kxlow_orders.py:*)'
)

function Get-EtNow {
    [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Eastern Standard Time')
}
$Started = Get-Date
if (-not $Day) { $Day = (Get-EtNow).Date.AddDays(-1).ToString('yyyy-MM-dd') }
# IMM_REVIEW_DIR (imm_review.py honours it too) points a test at a scratch folder.
$ReviewRoot = $env:IMM_REVIEW_DIR
if (-not $ReviewRoot) { $ReviewRoot = Join-Path $Repo "run-logs\incentive-mm\daily_review" }
$Dir = Join-Path $ReviewRoot $Day
New-Item -ItemType Directory -Force $Dir | Out-Null
$LogPath = Join-Path $Dir "run.log"
function Log([string]$Line) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Line" | Add-Content -Path $LogPath -Encoding utf8
}

# Task Scheduler sessions do not reliably carry user env vars: take them from
# HKCU (run_kalshi_daily_recap.ps1). The user site holds requests etc.
foreach ($n in 'ALERT_EMAIL_FROM', 'ALERT_EMAIL_PASSWORD', 'ALERT_EMAIL_TO', 'IMM_REVIEW_TO',
               'KALSHI_PRIVATE_KEY_PATH', 'IMM_KALSHI_API_KEY_ID', 'IMM_KALSHI_PRIVATE_KEY_PATH') {
    if (-not (Get-Item "env:$n" -ErrorAction SilentlyContinue)) {
        try { Set-Item "env:$n" (Get-ItemProperty -Path "HKCU:\Environment" -Name $n -ErrorAction Stop).$n } catch {}
    }
}
if ((Test-Path $UserSite) -and (($env:PYTHONPATH -split ';') -notcontains $UserSite)) {
    $env:PYTHONPATH = (@($UserSite) + @($env:PYTHONPATH -split ';' | Where-Object { $_ })) -join ';'
}
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONDONTWRITEBYTECODE = "1"

# ONE-YEAR LOGIN, read exactly as run_kalshi_daily_recap.ps1 reads it: the
# `claude setup-token` token in ~/.claude/imm-research-token.txt, for THIS
# process only; the API-key variables would outrank it and bill the API, so
# they are dropped. No file or no token = the stored login. Never logged.
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
    if ($LASTEXITCODE -eq 0 -and $out -match 'OK') { "review login OK via $Auth"; exit 0 }
    "review login FAILED via ${Auth}: $out"
    exit 1
}

function Send-Alert([string]$Subject, [string]$Body) {
    if ($NoEmail) { Log "alert (not sent, -NoEmail): $Subject"; return }
    $o = & $Python (Join-Path $Repo "send_alert_email.py") $Subject $Body.Replace('"', "'") 2>&1 | ForEach-Object { "$_" }
    Log "alert email: $($o -join ' | ')"
}

# One child process with a time limit; stdout/stderr captured, stdin optional.
function Invoke-Proc([string]$Exe, [string]$Arguments, [int]$TimeoutMin, [string]$StdIn = $null) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = $Arguments
    $psi.WorkingDirectory = $CodeDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    $p = [System.Diagnostics.Process]::Start($psi)
    try { $p.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::BelowNormal } catch {}
    if ($StdIn) { $p.StandardInput.Write($StdIn) }
    $p.StandardInput.Close()
    $so = $p.StandardOutput.ReadToEndAsync()
    $se = $p.StandardError.ReadToEndAsync()
    $exited = $p.WaitForExit($TimeoutMin * 60 * 1000)
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

# The tripwire: dirty + untracked files outside run-logs, with a content hash.
function Get-TreeState {
    $state = @{}
    $lines = & git -C $Repo status --porcelain=v1 --untracked-files=all 2>$null
    foreach ($l in $lines) {
        if ($l.Length -lt 4) { continue }
        $p = $l.Substring(3).Trim().Trim('"')
        if ($p -like 'run-logs/*' -or $p -match '__pycache__' -or $p -match '\.pyc$') { continue }
        $full = Join-Path $Repo ($p -replace '/', '\')
        $h = 'gone'
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            try { $h = (Get-FileHash -LiteralPath $full -Algorithm SHA1).Hash } catch { $h = 'locked' }
        }
        $state[$p] = $l.Substring(0, 2) + ' ' + $h
    }
    $state
}

$Marker = Join-Path $Dir "sent.marker"
if ((Test-Path $Marker) -and -not $Force -and -not $NoEmail) {
    Log "already sent for $Day ($Marker); -Force to run again"
    exit 0
}
Log "=== imm daily review for $Day start (auth: $Auth, model $Model) ==="
$HeadBefore = (& git -C $Repo rev-parse --short HEAD 2>$null)
$Before = Get-TreeState

# 1. evidence
$c = Invoke-Proc $Python "imm_review.py collect --day $Day" $CollectTimeoutMin
Log "collect exit=$($c.ExitCode) timedout=$($c.TimedOut): $(($c.Out + $c.Err).Trim() -replace '\s+', ' ')"
$Evidence = Join-Path $Dir "evidence.md"
$Reason = ""
if (-not (Test-Path $Evidence)) {
    $Reason = "the evidence pack failed to build"
    Send-Alert "IMM review $Day FAILED: no evidence pack" "imm_review.py collect exit=$($c.ExitCode) timedout=$($c.TimedOut)`n`n$(($c.Out + $c.Err).Trim())`n`nLog: $LogPath"
}

# 2. the analysis
$Findings = Join-Path $Dir "findings.json"
$Review = Join-Path $Dir "review.md"
if (-not $SkipClaude -and -not $Reason) {
    $weekly = ""
    if ((Get-EtNow).DayOfWeek -eq 'Sunday') {
        $weekly = " This is the SUNDAY run, so also do the weekly pass (SKILL.md section 7)."
    }
    $prompt = @"
Run the imm-daily-review skill in SCHEDULED mode for ET day $Day.$weekly
Read $Skill first and follow it exactly; reference.md and history.md sit beside it.
Run every command from $CodeDir (this overrides the folder named in SKILL.md, if they differ).
This is an unattended run from Windows Task Scheduler (run_imm_daily_review.ps1). Nobody can answer questions: make reasonable choices and say what you assumed.
The evidence pack is already built: $Dir\evidence.md (evidence.json beside it).
Edit and Write are switched off. Write your two outputs only with the put command and a heredoc, for example:
  python imm_review.py put findings.json --day $Day <<'EOF'
  { ... }
  EOF
then run: python imm_review.py ledger ingest --day $Day
then write review.md the same way: python imm_review.py put review.md --day $Day <<'EOF' ... EOF
Every run answers one question for itself (SKILL.md step 8, Jack 2026-11-26), in findings.json "compound": What else should this review be doing, daily, to compound and improve itself? Evidence section 11 is its input. At least one answer, every day.
Every IMM change is measured at 7 days (evidence section 12, SKILL.md step 7, Jack 2026-11-26): in findings.json "changes", scope what went live on the day and judge every readout listed today.
Do not send anything and do not run any send_*.py: this launcher sends the email when you finish.
Finish with at most 5 lines: the verdict, the decisions, and anything that blocked you.
"@
    $quoted = ($Deny | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $ClaudeArgs = "-p --model $Model --permission-mode bypassPermissions --strict-mcp-config --no-session-persistence --output-format stream-json --verbose --disallowedTools $quoted"
    for ($try = 1; $try -le 2; $try++) {
        $r = Invoke-Proc $Claude $ClaudeArgs $ClaudeTimeoutMin $prompt
        $suffix = ""
        if ($try -gt 1) { $suffix = "_try$try" }
        $stream = Join-Path $Dir "claude_stream$suffix.jsonl"
        [System.IO.File]::WriteAllText($stream, $r.Out, (New-Object System.Text.UTF8Encoding $false))
        $result = ""
        foreach ($line in ($r.Out -split "`n")) {
            if ($line -match '"type"\s*:\s*"result"') {
                try { $o = $line | ConvertFrom-Json; $result = "$($o.result)"; Log "claude result: turns=$($o.num_turns) secs=$([int]($o.duration_ms / 1000)) error=$($o.is_error)" } catch {}
            }
        }
        Log "claude try $try exit=$($r.ExitCode) timedout=$($r.TimedOut); stderr: $(($r.Err).Trim() -replace '\s+', ' ')"
        if ($result) { Log "claude says: $($result -replace '\s+', ' ')" }
        $tail = (($r.Err + "`n" + $result).Trim())
        if ($tail -match $LoginError) {
            $Reason = "Claude Code login failed ($Auth)"
            Send-Alert "IMM review $Day FAILED: Claude login" ("The review's analysis could not log in ($Auth).`n`nLOGIN: run 'claude setup-token' in a terminal and paste the token into $TokenFile (one-year login), then test: powershell -File $PSCommandPath -CheckLogin`n`n" + $tail + "`n`nLog: $LogPath")
            break
        }
        if (Test-Path $Review) { break }
        $elapsed = ((Get-Date) - $Started).TotalMinutes
        if ($try -eq 1 -and $elapsed -lt 50) {
            Log "no review.md after try 1 ($([int]$elapsed) min in); retrying in 5 min"
            Start-Sleep -Seconds 300
        } else {
            $Reason = if ($r.TimedOut) { "the analysis timed out after $ClaudeTimeoutMin min" } else { "the analysis wrote no review (claude exit $($r.ExitCode))" }
            break
        }
    }
}
if ($SkipClaude -and -not $Reason) { $Reason = "analysis skipped (-SkipClaude)" }

# 3. tripwire
$After = Get-TreeState
$HeadAfter = (& git -C $Repo rev-parse --short HEAD 2>$null)
$changed = @()
foreach ($k in $After.Keys) {
    if (-not $Before.ContainsKey($k) -or $Before[$k] -ne $After[$k]) { $changed += "$($After[$k].Substring(0, 2)) $k" }
}
if ($changed.Count -gt 0) {
    Log "TRIPWIRE: live tree changed during the run: $($changed -join ', ')"
    Send-Alert "IMM review $Day TRIPWIRE: live tree changed during the review run" ("These files outside run-logs appeared or changed while the review ran (HEAD $HeadBefore -> $HeadAfter). The review may not write there; another session may have. Check DIR\claude_stream.jsonl for any Edit/Write/Bash write.`n`n" + ($changed -join "`n") + "`n`nLog: $LogPath")
} else {
    Log "tripwire clean (HEAD $HeadBefore -> $HeadAfter)"
}

# 4. ingest (if the run did not) and send
if ((Test-Path $Findings) -and -not (Test-Path (Join-Path $Dir "ingested.json"))) {
    $i = Invoke-Proc $Python "imm_review.py ledger ingest --day $Day" 5
    Log "ingest exit=$($i.ExitCode): $(($i.Out + $i.Err).Trim() -replace '\s+', ' ')"
}
$sendArgs = "send_imm_review.py --day $Day"
if ($NoEmail) { $sendArgs += " --dry" }
if ($Force) { $sendArgs += " --force" }
if (-not (Test-Path $Review) -or $Reason) {
    if (-not $Reason) { $Reason = "no review.md" }
    $sendArgs += " --fallback --reason `"$($Reason.Replace('"', "'"))`""
}
$s = Invoke-Proc $Python $sendArgs 15
Log "send exit=$($s.ExitCode): $(($s.Out + $s.Err).Trim() -replace '\s+', ' ')"
$code = 0
if ($s.ExitCode -ne 0) {
    $code = 1
    $logTail = (Get-Content $LogPath -Tail 12) -join "`n"
    Send-Alert "IMM review $Day FAILED: email not sent" "send_imm_review.py exit=$($s.ExitCode)`n`n$logTail"
} elseif ($Reason -and -not $SkipClaude) {
    $code = 2   # the fallback went out; Task Scheduler shows a non-zero result
}
Log "=== done in $([int]((Get-Date) - $Started).TotalMinutes) min, exit=$code ==="
exit $code
