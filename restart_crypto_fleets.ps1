# Restart all three crypto fleets (touch x16, updown x7, annual x8) to pick up
# code changes. Follows CRYPTO_MM_HANDOFF.md: stop tasks -> kill whole chains ->
# clean stale locks and __pycache__ -> start tasks -> repair locks.
# Kill list is built from the bots' own lock_*.pid files (authoritative),
# ancestors resolved via ParentProcessId (CommandLine is null from the agent
# shell - documented trap). Launchers die before pythons so nothing respawns
# mid-pass. Lock cleanup only removes locks whose pid is DEAD: deleting a live
# bot's lock strips its singleton protection (7 markets ran lockless after the
# 2026-09-01 restart until an audit pass rewrote them).

param(
    # all | touch | updown | annual — scope the restart to one fleet
    [ValidateSet('all','touch','updown','annual')]
    [string]$Fleet = 'all'
)

$ErrorActionPreference = 'Continue'
$repo = 'C:\Users\jackd\Documents\KL'
$allowed = @('python','cmd','powershell','pwsh','wscript')
# kill launchers first so the while-loop can't respawn a python mid-pass
$killRank = @{ 'powershell' = 0; 'pwsh' = 0; 'cmd' = 1; 'wscript' = 2; 'python' = 3 }
$fleetDefs = @{
    touch  = @{ task = 'KL crypto_touch_mm *';  dir = "$repo\run-logs\crypto-touch"
                markets = @('BNB-MAX','BNB-MIN','BTC-MAX','BTC-MIN','DOGE-MAX','DOGE-MIN','ETH-MAX','ETH-MIN','HYPE-MAX','HYPE-MIN','SOL-MAX','SOL-MIN','XRP-MAX','XRP-MIN','ZEC-MAX','ZEC-MIN') }
    updown = @{ task = 'KL crypto_updown_mm *'; dir = "$repo\run-logs\crypto-updown"
                markets = @('BNB','BTC','DOGE','ETH','HYPE','SOL','XRP') }
    annual = @{ task = 'KL crypto_annual_mm *'; dir = "$repo\run-logs\crypto-annual"
                markets = @('BNB','BTC','DOGE','ETH','HYPE','SOL','XRP','ZEC') }
}
$active = if ($Fleet -eq 'all') { @('touch','updown','annual') } else { @($Fleet) }
$lockGlobs = $active | ForEach-Object { "$($fleetDefs[$_].dir)\lock_*.pid" }
$expected = ($active | ForEach-Object { $fleetDefs[$_].markets.Count } | Measure-Object -Sum).Sum
Write-Host "fleet scope: $($active -join '+') ($expected bots)"

function Get-LivePython([int]$p) {
    $proc = Get-Process -Id $p -ErrorAction SilentlyContinue
    return ($proc -and $proc.ProcessName -eq 'python')
}

# --- my own ancestry: never kill it -----------------------------------------
$mine = @(); $p = $PID
for ($i=0; $i -lt 8 -and $p; $i++) {
    $mine += $p
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue
    if (-not $proc) { break }
    $p = $proc.ParentProcessId
}
Write-Host "own ancestry (spared): $($mine -join ',')"

# --- 1. stop the bot tasks ---------------------------------------------------
$patterns = $active | ForEach-Object { $fleetDefs[$_].task }
$tasks = Get-ScheduledTask | Where-Object {
    $t = $_.TaskName
    (($patterns | Where-Object { $t -like $_ }) -ne $null) -and
    ($t -notlike '*DIGEST*') -and ($t -notlike '*WATCHDOG*')
}
Write-Host "stopping $($tasks.Count) tasks..."
$tasks | ForEach-Object { Stop-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

# --- 2. kill chains from lock pids, several passes ---------------------------
for ($pass=1; $pass -le 4; $pass++) {
    $targets = New-Object System.Collections.Generic.HashSet[int]
    foreach ($lf in (Get-ChildItem $lockGlobs -ErrorAction SilentlyContinue)) {
        $lpid = 0
        if (-not [int]::TryParse((Get-Content $lf.FullName -ErrorAction SilentlyContinue | Select-Object -First 1), [ref]$lpid)) { continue }
        $cur = $lpid
        for ($d=0; $d -lt 6 -and $cur; $d++) {
            if ($mine -contains $cur) { break }
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$cur" -ErrorAction SilentlyContinue
            if (-not $proc) { break }
            $name = $proc.Name -replace '\.exe$',''
            if ($allowed -notcontains $name) { break }
            [void]$targets.Add($cur)
            $cur = $proc.ParentProcessId
        }
    }
    if ($targets.Count -eq 0) { Write-Host "pass ${pass}: nothing left to kill"; break }
    $order = $targets | Sort-Object {
        $proc = Get-Process -Id $_ -ErrorAction SilentlyContinue
        if ($proc -and $killRank.ContainsKey($proc.ProcessName)) { $killRank[$proc.ProcessName] } else { 9 }
    }
    $killed = 0
    foreach ($t in $order) {
        $proc = Get-Process -Id $t -ErrorAction SilentlyContinue
        if (-not $proc) { continue }
        if ($allowed -notcontains $proc.ProcessName) { continue }
        try { Stop-Process -Id $t -Force -ErrorAction Stop; $killed++ } catch {}
    }
    Write-Host "pass ${pass}: killed $killed"
    Start-Sleep -Seconds 5
}

# --- 3. STALE locks only + __pycache__ ---------------------------------------
$removed = 0
foreach ($lf in (Get-ChildItem $lockGlobs -ErrorAction SilentlyContinue)) {
    $lpid = 0
    [void][int]::TryParse((Get-Content $lf.FullName -ErrorAction SilentlyContinue | Select-Object -First 1), [ref]$lpid)
    if (-not (Get-LivePython $lpid)) { Remove-Item $lf.FullName -Force -ErrorAction SilentlyContinue; $removed++ }
}
Remove-Item "$repo\__pycache__" -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "stale locks removed: $removed; __pycache__ cleaned"

# --- 4. start tasks ----------------------------------------------------------
Start-Sleep -Seconds 2
$tasks | ForEach-Object { Start-ScheduledTask -TaskName $_.TaskName -ErrorAction SilentlyContinue }
Write-Host "started $($tasks.Count) tasks; waiting out launcher jitter (90s)..."
Start-Sleep -Seconds 90

# --- 5. lock repair: a bot whose lock a race deleted never rewrites it -------
$repaired = 0; $lockCount = 0
foreach ($fname in $active) {
    $dir = $fleetDefs[$fname].dir
    foreach ($m in $fleetDefs[$fname].markets) {
        $lock = Join-Path $dir "lock_$m.pid"
        $status = Join-Path $dir "status_$m.json"
        if (Test-Path $lock) { $lockCount++; continue }
        try { $spid = [int](Get-Content $status -Raw | ConvertFrom-Json).pid } catch { continue }
        if (Get-LivePython $spid) {
            Set-Content -Path $lock -Value $spid -NoNewline
            $repaired++; $lockCount++
            Write-Host "repaired lock: $m -> $spid"
        }
    }
}

# --- 6. census ---------------------------------------------------------------
$py = @(Get-Process python -ErrorAction SilentlyContinue).Count
Write-Host ""
Write-Host "pythons now: $py (expected: restarted fleet(s) $expected + everything out of scope)"
Write-Host "fleet locks now: $lockCount / $expected (repaired: $repaired)"
$tasks | ForEach-Object { Get-ScheduledTask -TaskName $_.TaskName } |
    Group-Object State | ForEach-Object { Write-Host ("tasks {0}: {1}" -f $_.Name, $_.Count) }
Write-Host "if locks < ${expected}: a bot may still be inside launcher jitter - rerun steps 5-6 in a minute"
