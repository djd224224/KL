# Switches the LIVE crypto_updown_mm fleet from weekly-only to daily+weekly.
# NEEDS ELEVATION - run via enable_updown_daily.bat (self-elevates).
#
# Cadence is baked into each task's action (vbs argument), so this is a task
# RE-REGISTRATION, which is only safe with the fleet fully stopped: the
# 2026-08-09 incident showed that re-registering a Running task orphans its
# launcher chain, which keeps trading outside task control. Procedure, per
# CRYPTO_MM_HANDOFF's restart recipe (hardened 2026-08-14 after adversarial
# review):
#   0. DISABLE all 7 tasks first. The 15-min watchdog starts any READY
#      'KL crypto_updown_mm *' task; without this step it can revive an old
#      weekly task mid-procedure and Register -Force then orphans that fresh
#      chain - the exact 8/9 mechanism. Disabled tasks are skipped.
#   1. Stop the tasks (kills each task's wscript).
#   2. Kill surviving chain members (powershell/cmd/python outlive the
#      wscript). Status-file PIDs are trusted only with an identity check:
#      fresh heartbeat + python image name + (when CommandLine is readable)
#      an updown command line - Windows recycles PIDs and a stale status
#      file must never kill a sibling fleet's chain. Ancestors are killed
#      only when they look like launcher links (cmd/powershell/wscript,
#      started BEFORE the child - PID reuse forges PPID links). A chain
#      under claude.exe is killed only if its own command line proves it is
#      an updown chain; otherwise listed and left for the operator.
#   3. Verify zero updown pythons remain (abort + restore if not).
#   4. register_updown_tasks.ps1 - re-registers with daily,weekly + starts.
#      Its exit code is CHECKED; on failure the old tasks are re-enabled and
#      restarted so the fleet is never silently left dark.
# Resting orders from the killed bots TTL out within 600s, each restarted
# bot's startup sweep cancels leftovers, and the bots now hold per-asset
# singleton locks as a last line against any orphan this misses.

$ErrorActionPreference = "Continue"
$Repo = "C:\Users\jackd\Documents\KL"
$StatusDir = Join-Path $Repo "run-logs\crypto-updown"
$TaskGlob = "KL crypto_updown_mm *"

function Get-Proc([uint32]$ProcessId) {
    Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
}

function Test-UpdownCmdline([object]$Proc) {
    # $null CommandLine (unreadable in some contexts) => "unknown", not "no".
    if (-not $Proc.CommandLine) { return $null }
    return ($Proc.CommandLine -like "*crypto_updown_mm*")
}

function Get-AncestorNames([uint32]$ProcessId) {
    $names = @(); $current = $ProcessId; $hops = 0
    while ($current -and $hops -lt 10) {
        $p = Get-Proc $current
        if (-not $p) { break }
        $names += $p.Name; $current = $p.ParentProcessId; $hops++
    }
    return $names
}

function Restore-OldFleet {
    Write-Host "RESTORING: re-enabling + starting the previous tasks so the fleet is not left dark."
    Get-ScheduledTask -TaskName $TaskGlob -ErrorAction SilentlyContinue | Enable-ScheduledTask | Out-Null
    Get-ScheduledTask -TaskName $TaskGlob -ErrorAction SilentlyContinue | Start-ScheduledTask
}

Write-Host "0) Disabling the updown tasks (watchdog starts READY tasks; Disabled are skipped)..."
Get-ScheduledTask -TaskName $TaskGlob -ErrorAction SilentlyContinue | Disable-ScheduledTask | Out-Null

Write-Host "1) Stopping the updown tasks..."
Get-ScheduledTask -TaskName $TaskGlob -ErrorAction SilentlyContinue |
    Where-Object State -eq "Running" | Stop-ScheduledTask
Start-Sleep -Seconds 3

Write-Host "2) Killing surviving launcher chains..."
$targets = @()
# (a) bot pythons by status-file PID - identity-checked (see header)
foreach ($f in Get-ChildItem (Join-Path $StatusDir "status_*.json") -ErrorAction SilentlyContinue) {
    if (((Get-Date) - $f.LastWriteTime).TotalMinutes -gt 15) {
        Write-Host "   ignoring stale status file $($f.Name) (heartbeat > 15 min old)"
        continue
    }
    try { $st = Get-Content $f.FullName -Raw | ConvertFrom-Json } catch { continue }
    if (-not $st.pid) { continue }
    $p = Get-Proc ([uint32]$st.pid)
    if (-not $p) { continue }
    if ($p.Name -notlike "python*") {
        Write-Host "   ignoring status pid $($st.pid): image is $($p.Name), not python (PID recycled)"
        continue
    }
    if ((Test-UpdownCmdline $p) -eq $false) {
        Write-Host "   ignoring status pid $($st.pid): command line is not an updown bot (PID recycled)"
        continue
    }
    $targets += [uint32]$st.pid
}
# (b) anything whose command line names the updown launcher/module
$targets += (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*run_crypto_updown_mm*" -or
                   $_.CommandLine -like "*crypto_updown_mm.py*" }).ProcessId

$killed = 0
$spared = @()
foreach ($id in ($targets | Sort-Object -Unique)) {
    $p = Get-Proc $id
    if (-not $p) { continue }
    if (((Get-AncestorNames $id) -contains "claude.exe") -and ((Test-UpdownCmdline $p) -ne $true)) {
        # Under a Claude session AND not provably an updown chain: never kill
        # a session's own shell. A REAL bot chain is a kill target no matter
        # who spawned it, so a matching command line proceeds.
        $spared += $id
        Write-Host "   skipping pid $id ($($p.Name)): claude.exe ancestry, not provably updown"
        continue
    }
    # kill the whole chain upward: python -> cmd -> powershell -> wscript.
    # Each ancestor must look like a launcher link AND predate its child
    # (PID reuse forges PPID links; a recycled 'parent' is YOUNGER).
    $chain = @($id); $current = $p.ParentProcessId; $childStart = $p.CreationDate
    $hops = 0
    while ($current -and $hops -lt 6) {
        $parent = Get-Proc $current
        if (-not $parent -or $parent.Name -notin @("cmd.exe", "powershell.exe", "wscript.exe")) { break }
        if ($parent.CreationDate -and $childStart -and $parent.CreationDate -gt $childStart) {
            Write-Host "   not walking above pid $current (parent younger than child: PID reuse)"
            break
        }
        if ((Test-UpdownCmdline $parent) -eq $false) { break }   # readable and NOT ours
        $chain += $parent.ProcessId
        $childStart = $parent.CreationDate
        $current = $parent.ParentProcessId; $hops++
    }
    foreach ($cid in $chain) {
        try { Stop-Process -Id $cid -Force -ErrorAction Stop; $killed++ } catch {}
    }
}
Write-Host "   killed $killed process(es)"
Start-Sleep -Seconds 2

Write-Host "3) Verifying no updown bot process survives..."
$alive = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { ($_.Name -like "python*" -and $_.CommandLine -like "*crypto_updown_mm.py*") -or
                   $_.CommandLine -like "*run_crypto_updown_mm*" })
if ($alive.Count -gt 0) {
    Write-Host "ABORTING: $($alive.Count) updown process(es) still alive:"
    # NB: this runs under powershell.exe 5.1 (the .bat) - no ?? / ternary here.
    $alive | ForEach-Object {
        $cl = $_.CommandLine
        if (-not $cl) { $cl = "<no cmdline>" }
        Write-Host ("   pid {0} {1}: {2}" -f $_.ProcessId, $_.Name,
            $cl.Substring(0, [Math]::Min(120, $cl.Length)))
    }
    if ($spared.Count -gt 0) { Write-Host "   (spared for claude.exe ancestry: $($spared -join ', '))" }
    Write-Host "Kill them by hand, then re-run this script."
    Restore-OldFleet
    exit 1
}

Write-Host "4) Re-registering with cadence daily,weekly + starting..."
& (Join-Path $Repo "register_updown_tasks.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "FAILED: register_updown_tasks.ps1 exited $LASTEXITCODE - the fleet was NOT re-armed."
    Restore-OldFleet
    exit 1
}
Write-Host ""
Write-Host "SUCCESS. Check a banner in run-logs\crypto-updown\*-daily+weekly-*.log:"
Write-Host "it must show 'cadence=daily,weekly' and 'ladder 3x2 (daily 3x1)'."
