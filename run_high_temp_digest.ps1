# Launcher for the high-temp (KXHIGH) daily digest under Windows Task
# Scheduler: "KL high-temp-digest", 6:12 PM daily, through
# run_high_temp_digest_hidden.vbs so no console window shows.
#
# It replaces the Claude Desktop scheduled task "high-temp-daily-digest"
# (disabled 2026-10-12, Jack: "Yes, build it"). Every desktop run since 7/27
# stalled on a permission prompt (bq, Write, a chained cd), and the app starts
# no new run while one is still "running", so one stalled run blocked the
# rest for days (10/7 -> 10/11). Same pattern as run_kalshi_daily_recap.ps1:
# claude.exe -p with bypassPermissions, the one-year token file, an email on
# failure. The prompt is still that task's SKILL.md, with the overrides in
# $PromptTemplate below.
#
# A run:
#  1. D = the day whose digest is due: yesterday (ET) from 6 PM ET on, the
#     day before that earlier in the day. A run that StartWhenAvailable fires
#     the next morning still writes the day it missed.
#  2. Waits for the evening BigQuery load. The "5 PM ET" cron in
#     run_high_temp_monitoring.yml (21:00Z) has really started 7:15-9:35 PM
#     ET, and 10:17 PM on 10/12; D's settlements are not in BigQuery before
#     it. Checked every 10 min for up to 7 h; after that the digest is
#     written with estimates.
#  3. claude -p writes run-logs\high-temp\<D>.html in its own worktree,
#     $Wt (detached at origin/main), never in KL, the live bot's checkout.
#  4. This script, not Claude, checks the file, updates latest.html, commits
#     the two files and pushes to origin/main. "KL sync-kl-main" brings them
#     into KL at :15/:45.
#  5. Emails the digest (send_html_email.py). A failure emails the log tail.
#
#   run_high_temp_digest.ps1                                        # the scheduled run
#   run_high_temp_digest.ps1 -Date 2026-10-03,2026-10-04 -NoEmail   # backfill, oldest first
#   run_high_temp_digest.ps1 -CheckLogin                            # test the login only
#   -NoWait skips the BigQuery wait.
#
# Logs to run-logs\high-temp-digest\YYYY-MM-DD.log (the day of the run).

param(
    [string[]]$Date,
    [switch]$NoWait,
    [switch]$NoEmail,
    [switch]$CheckLogin
)

$Claude = "C:\Users\jackd\AppData\Local\Microsoft\WinGet\Packages\Anthropic.ClaudeCode_Microsoft.Winget.Source_8wekyb3d8bbwe\claude.exe"
$Repo = "C:\Users\jackd\Documents\KL"
$Wt = "C:\Users\jackd\Documents\KL-wt-high-temp-digest"
$Skill = "C:\Users\jackd\.claude\scheduled-tasks\high-temp-daily-digest\SKILL.md"
$Python = "C:\Users\jackd\AppData\Local\Programs\Python\Python312\python.exe"
$SdkBin = "C:\Users\jackd\tools\google-cloud-sdk\bin"
$ClaudeTimeoutMin = 45
$LoadWaitHours = 7
$LoadPollMin = 10

$LogDir = Join-Path $Repo "run-logs\high-temp-digest"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$LogPath = Join-Path $LogDir ("{0:yyyy-MM-dd}.log" -f (Get-Date))
function Log([string]$Line) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Line" | Add-Content -Path $LogPath -Encoding utf8
}

# Task Scheduler sessions do not reliably inherit user env vars (see
# run_kalshi_daily_recap.ps1): take them from HKCU.
foreach ($n in 'CLOUDSDK_PYTHON','ALERT_EMAIL_FROM','ALERT_EMAIL_PASSWORD','ALERT_EMAIL_TO') {
    if (-not (Get-Item "env:$n" -ErrorAction SilentlyContinue)) {
        try { Set-Item "env:$n" (Get-ItemProperty -Path "HKCU:\Environment" -Name $n -ErrorAction Stop).$n } catch {}
    }
}
if (($env:Path -split ';') -notcontains $SdkBin) { $env:Path = "$SdkBin;$env:Path" }
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
        "digest login OK via $Auth"
        exit 0
    }
    "digest login FAILED via ${Auth}: $out"
    exit 1
}

function Invoke-Git {
    $out = (& git @args 2>&1 | ForEach-Object { "$_" }) -join "`n"
    [pscustomobject]@{ Ok = ($LASTEXITCODE -eq 0); Out = $out }
}

function Get-EtNow {
    [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTime]::UtcNow, 'Eastern Standard Time')
}

# The evening load re-pulls the whole settlements table, so every row carries
# that run's pulled_ts. D's markets settle 11:00-14:15Z on D+1; a pull after
# 16:00Z on D+1 has them.
function Get-LoadState([datetime]$Day) {
    $cut = $Day.AddDays(1).ToString('yyyy-MM-dd') + ' 16:00:00+00'
    $sql = "SELECT CAST(MAX(pulled_ts) >= TIMESTAMP('$cut') AS STRING) AS ready, CAST(MAX(pulled_ts) AS STRING) AS last_pull FROM Kalshi.KXHIGH_settlements_clean"
    $out = @(& bq.cmd --project_id=elite-contact-446323-q7 --location=northamerica-northeast1 --format=csv --quiet query --use_legacy_sql=false $sql 2>&1 | ForEach-Object { "$_" })
    $row = $out | Where-Object { $_ -match '^(true|false),' } | Select-Object -Last 1
    if ($row) {
        $f = $row -split ',', 2
        return [pscustomobject]@{ Ready = ($f[0] -eq 'true'); LastPull = $f[1]; Error = "" }
    }
    [pscustomobject]@{ Ready = $false; LastPull = "?"; Error = ($out -join ' | ') }
}

function Reset-Worktree {
    $f = Invoke-Git -C $Repo fetch origin
    if (-not $f.Ok) {
        Start-Sleep -Seconds 20   # the sync's own fetch may hold the ref lock
        $f = Invoke-Git -C $Repo fetch origin
    }
    if (-not $f.Ok) { throw "git fetch failed: $($f.Out)" }
    if (-not (Test-Path (Join-Path $Wt ".git"))) {
        $a = Invoke-Git -C $Repo worktree add --detach $Wt origin/main
        if (-not $a.Ok) { throw "git worktree add $Wt failed: $($a.Out)" }
        Log "created worktree $Wt"
        return
    }
    Invoke-Git -C $Wt rebase --abort | Out-Null   # a run killed mid-rebase
    $c = Invoke-Git -C $Wt checkout --detach --force origin/main
    if (-not $c.Ok) { throw "worktree checkout failed: $($c.Out)" }
    Invoke-Git -C $Wt clean -fdq | Out-Null   # the last run's scratch files
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

$PromptTemplate = @'
Write the KXHIGH high-temp daily digest for D = {D}.

This is an unattended run from Windows Task Scheduler (run_high_temp_digest.ps1 in the KL repo). Nobody can answer questions, so make reasonable choices and note them. The task is defined in {SKILL}. Read that file first and follow it, except where the overrides below differ. The overrides win.

OVERRIDES

1. Target day: D = {D}. Do not work it out from the clock.

2. Where to work: your working directory, {WT} ({WTBASH} in bash), is a git worktree of the KL repo checked out at origin/main. The template is run-logs/high-temp/2026-04-18.html there. Write exactly one file: run-logs/high-temp/{D}.html. Do not write latest.html or latest.md; the launcher updates latest.html. Never cd into, read or write C:\Users\jackd\Documents\KL: it is the live trading bot's checkout.

3. No git and no browser: skip the SKILL.md sections "Commit and push" and "Open the dashboard". Run no git command that changes anything (add, commit, push, pull, fetch, merge, rebase, checkout, switch, reset, restore, stash, clean). The launcher checks your file, commits and pushes it, and emails it.

4. P&L basis: BigQuery's KXHIGH views book every NO fill at the YES price. In KXHIGH_fills_enriched, price_paid_dollars, fill_cost_dollars and model_edge_at_fill are wrong for NO fills; in KXHIGH_settlements_clean, total_cost_dollars and pnl_dollars are wrong. Use Kalshi's basis everywhere:
   - Settled market: P&L = revenue_dollars - yes_total_cost_dollars - no_total_cost_dollars - fee_cost_dollars, and cost = yes_total_cost_dollars + no_total_cost_dollars (KXHIGH_settlements_clean). A market is a win when its P&L > 0.
   - Fills: the price paid is raw_no_price_dollars for side = 'no' and raw_yes_price_dollars for side = 'yes', in KXHIGH_fills_clean (fills_enriched does not have them; join on fill_id for its forecast columns). Cost = filled_count x that price.
   - Do not report model_edge_at_fill for NO fills.
   - Pending markets: estimate with the SKILL.md band formula, on this cost.
   - Check: for 2026-10-02 this basis gives -97.13 on 430.02 deployed, 9W/11L.
   Put one data-note banner under the header with the recorded figure (sum of pnl_dollars on sum of total_cost_dollars) next to Kalshi's, as run-logs/high-temp/2026-10-02.html does.

5. Data status: {LOADNOTE}

6. Final message: one short paragraph, as the SKILL.md asks. Then, as the very last line, exactly:
DIGEST_SUMMARY: <P&L on Kalshi's basis> on <capital deployed>, <W>W/<L>L, <settled | partly settled | estimated>
For example: DIGEST_SUMMARY: -$97.13 on $430.02, 9W/11L, settled
'@

# Writes, checks, commits and pushes one day's digest. Throws when no good
# digest was written; a digest that could not be pushed comes back with
# PushError set (and a copy in $LogDir), so it can still be emailed.
function Invoke-Digest([datetime]$Day, [string]$LoadNote) {
    $ds = $Day.ToString('yyyy-MM-dd')
    Reset-Worktree
    $wtBash = '/c' + $Wt.Substring(2).Replace('\', '/')
    $prompt = $PromptTemplate.Replace('{D}', $ds).Replace('{SKILL}', $Skill).Replace('{WTBASH}', $wtBash)
    $prompt = $prompt.Replace('{WT}', $Wt).Replace('{LOADNOTE}', $LoadNote)
    Log "${ds}: claude -p starting in $Wt (data status: $LoadNote)"
    $started = Get-Date
    $res = Invoke-Claude $prompt
    $mins = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
    Log "${ds}: claude exit=$($res.ExitCode) after $mins min$(if ($res.TimedOut) { ' (TIMED OUT, killed)' })"
    $res.Out.TrimEnd() | Add-Content -Path $LogPath -Encoding utf8
    if ($res.Err.Trim()) { "stderr: $($res.Err.TrimEnd())" | Add-Content -Path $LogPath -Encoding utf8 }
    if ($res.TimedOut) { throw "claude -p ran past $ClaudeTimeoutMin min and was killed" }
    if ($res.ExitCode -ne 0) { throw "claude -p exited $($res.ExitCode)" }

    $rel = "run-logs/high-temp/$ds.html"
    $file = Join-Path $Wt $rel
    if (-not (Test-Path $file)) { throw "claude -p finished but wrote no $rel" }
    $html = [System.IO.File]::ReadAllText($file)
    if ($html.Length -lt 5000 -or -not $html.Contains($ds)) {
        throw "$rel looks incomplete ($($html.Length) chars)"
    }
    $m = [regex]::Matches($res.Out, '(?m)^\s*DIGEST_SUMMARY:\s*(.+?)\s*$')
    $summary = if ($m.Count) { $m[$m.Count - 1].Groups[1].Value } else { "" }

    # latest.html follows the newest dated digest, so a backfill leaves it alone
    $paths = @($rel)
    $newest = Get-ChildItem (Join-Path $Wt "run-logs\high-temp") -Filter "????-??-??.html" |
        Sort-Object Name | Select-Object -Last 1
    if ($newest.Name -eq "$ds.html") {
        Copy-Item $file (Join-Path $Wt "run-logs\high-temp\latest.html") -Force
        $paths += "run-logs/high-temp/latest.html"
    }

    # A local copy of these paths in KL (untracked or modified) would make the
    # sync's fast-forward fail and hold up every later deploy: don't push then.
    $kl = Invoke-Git --no-optional-locks -C $Repo status --porcelain --untracked-files=all '--' @paths
    if ($kl.Out.Trim()) {
        Copy-Item $file (Join-Path $LogDir "$ds.html") -Force
        return [pscustomobject]@{ File = $file; Summary = $summary;
            PushError = "not pushed: KL has its own copy (git status: $($kl.Out.Trim())), which would stop the sync's fast-forward. Copy kept in $LogDir" }
    }
    Invoke-Git -C $Wt add '--' @paths | Out-Null
    $cm = Invoke-Git -C $Wt commit -q -m "High-temp digest for $ds" -m "Written by run_high_temp_digest.ps1 (Windows task KL high-temp-digest)."
    if (-not $cm.Ok) {
        if ($cm.Out -match 'nothing to commit|nothing added') {
            Log "${ds}: digest unchanged from origin/main, nothing to push"
            return [pscustomobject]@{ File = $file; Summary = $summary; PushError = "" }
        }
        throw "git commit failed: $($cm.Out)"
    }
    $push = $null
    for ($i = 1; $i -le 3; $i++) {
        $push = Invoke-Git -C $Wt push origin HEAD:refs/heads/main
        if ($push.Ok) { break }
        Log "${ds}: push attempt $i rejected: $($push.Out)"
        Invoke-Git -C $Repo fetch origin | Out-Null
        $rb = Invoke-Git -C $Wt rebase origin/main
        if (-not $rb.Ok) {
            Invoke-Git -C $Wt rebase --abort | Out-Null
            break
        }
    }
    if (-not $push.Ok) {
        Copy-Item $file (Join-Path $LogDir "$ds.html") -Force
        return [pscustomobject]@{ File = $file; Summary = $summary;
            PushError = "push to origin/main failed ($($push.Out)). Copy kept in $LogDir" }
    }
    $sha = (Invoke-Git -C $Wt rev-parse --short HEAD).Out
    Log "${ds}: pushed $sha to origin/main ($($paths -join ', '))"
    [pscustomobject]@{ File = $file; Summary = $summary; PushError = "" }
}

# ---------------------------------------------------------------- main

if ($Date) {
    $Days = @($Date -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ } |
        ForEach-Object { [datetime]::ParseExact($_, 'yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) } |
        Sort-Object -Unique)
} else {
    $Days = @((Get-EtNow).AddHours(-18).Date.AddDays(-1))
}
if (-not $Days) {
    Log "! no valid day in -Date '$Date' (format yyyy-MM-dd)"
    exit 2
}
$DayList = ($Days | ForEach-Object { $_.ToString('yyyy-MM-dd') }) -join ', '
Log "=== high-temp digest start: $DayList (auth: $Auth) ==="

# 1. the evening BigQuery load (the newest day's readiness covers the rest)
$state = Get-LoadState $Days[-1]
Log "BigQuery: settlements last pulled $($state.LastPull) UTC; loaded for $($Days[-1].ToString('yyyy-MM-dd')): $($state.Ready) $($state.Error)"
if (-not $state.Ready -and -not $NoWait) {
    $deadline = (Get-Date).AddHours($LoadWaitHours)
    while (-not $state.Ready -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds ($LoadPollMin * 60)
        $state = Get-LoadState $Days[-1]
        Log "BigQuery: settlements last pulled $($state.LastPull) UTC; loaded: $($state.Ready) $($state.Error)"
    }
}

# 2. one claude run per day, one launcher at a time (a backfill and the
# scheduled run share the worktree)
$mutex = New-Object System.Threading.Mutex($false, 'Global\KL-high-temp-digest')
$held = $false
try { $held = $mutex.WaitOne([TimeSpan]::FromHours(3)) } catch [System.Threading.AbandonedMutexException] { $held = $true }
if (-not $held) {
    Log "! another digest run held the lock for 3 h; giving up"
    Send-Alert "high-temp digest FAILED (lock)" "Another run_high_temp_digest.ps1 held the lock for 3 h; nothing written for $DayList. Log: $LogPath"
    exit 1
}
$failed = 0
try {
    foreach ($day in $Days) {
        $ds = $day.ToString('yyyy-MM-dd')
        $st = Get-LoadState $day
        $loadNote = if ($st.Ready) {
            "the evening BigQuery load after D's settlement has run (settlements last pulled $($st.LastPull) UTC), so D should be settled. If a D market is still missing from KXHIGH_settlements_clean, estimate it as the SKILL.md says and mark it (est.)."
        } elseif ($NoWait) {
            "the launcher did not wait for the evening BigQuery load (settlements last pulled $($st.LastPull) UTC). Where D's settlements are missing, use the SKILL.md's pending estimates and mark them (est.)."
        } else {
            "the evening BigQuery load had not run by $((Get-EtNow).ToString('h:mm tt')) ET (settlements last pulled $($st.LastPull) UTC), so D's settlements are probably not in BigQuery. Use the SKILL.md's pending estimates, mark them (est.), and say in the header that this digest was written before the load."
        }
        try {
            $r = Invoke-Digest $day $loadNote
        } catch {
            $failed++
            Log "! ${ds}: FAILED: $($_.Exception.Message)"
            $tail = (Get-Content $LogPath -Tail 12) -join "`n"
            $fix = ""
            if ($tail -match 'failed to authenticate|oauth (session|token)|not logged in|run /login|invalid api key') {
                $fix = "`n`nLOGIN ($Auth): run 'claude setup-token' in a terminal and paste the token over the contents of $TokenFile."
            }
            Send-Alert "high-temp digest FAILED for $ds" "run_high_temp_digest.ps1 wrote no digest for $ds ($($_.Exception.Message)). Log: $LogPath`n`n$tail$fix"
            continue
        }
        if ($r.PushError) {
            $failed++
            Log "! ${ds}: $($r.PushError)"
        }
        if (-not $NoEmail) {
            $subject = "High-temp digest $ds" + $(if ($r.Summary) { ": $($r.Summary)" } else { "" }) +
                $(if ($r.PushError) { " [NOT PUSHED]" } else { "" })
            $text = "High-temp digest for $ds. $($r.Summary)`n$($r.PushError)`nLog: $LogPath"
            $o = & $Python (Join-Path $PSScriptRoot "send_html_email.py") $subject.Replace('"', "'") $r.File $text.Replace('"', "'") 2>&1 | ForEach-Object { "$_" }
            Log "${ds}: digest email: $($o -join ' | ')"
        }
    }
} finally {
    $mutex.ReleaseMutex()
}
Log "=== high-temp digest done: $($Days.Count - $failed) of $($Days.Count) ok ==="
exit $(if ($failed) { 1 } else { 0 })
